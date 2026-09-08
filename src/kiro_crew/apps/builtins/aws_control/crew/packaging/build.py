"""``python -m packaging.build`` -- curate a local crew into a deployable bundle.

WHY THIS IS A PORT, NOT A COPY
------------------------------
``PACKAGING-CONTRACT.md`` (T1) says to port ``bundle.py`` + ``bundle_source.py``
from ``share-my-crew/build/serving/smc/`` and that those files "carry
``reviewed_by`` / ``reviewed_at`` and a content-hash recheck". Read in full,
they do NOT: ``serving/smc/bundle.py`` is the container's READER (it validates a
bundle at startup) and ``serving/smc/bundle_source.py`` is the S3 FETCH that the
top-level contract explicitly DELETES. Neither enumerates a crew, neither
curates, and neither carries a review signature or a content pin.

The deny-by-default producer the contract describes is
``share-my-crew/build/export/crew_export/`` -- ``candidates.py`` (enumeration,
everything starts excluded), ``plan.py`` (the ``reviewed_by`` / ``reviewed_at``
signature and the per-item sha256 content pin), ``spec.py`` (prompt inlining and
tool/MCP normalisation) and ``bundle.py`` (the layout writer and the digest the
contract points at: ``_bundle_digest``). This module ports THAT, because a port
of the named files would ship no curation at all -- and "a port that loosens
this is worse than no port".

The port is self-contained on purpose. ``crew_export`` imports
``kiro_crew.config.paths``, ``kiro_crew.knowledge.store``,
``kiro_crew.deploy.scan`` and ``kiro_crew.security``; NONE of those are importable
in this app's venv (it carries boto3 / fastapi / pydantic / pytest only, and no
PyYAML), so the curation plan is JSON rather than YAML and the credential
scanner is a self-contained subset of ``kiro_crew.deploy.scan`` -- see
``_HARD_PATTERNS`` and the report note about it.

THE DENY-BY-DEFAULT SEAM, PRESERVED
-----------------------------------
A skill or MCP server enters the bundle ONLY when a signed review says so and its
content still matches what was reviewed. Two guards, both from
``crew_export/plan.py``:

* **The signature.** ``reviewed_by`` and ``reviewed_at`` start blank; a review
  file that selects anything while either is blank is refused. There is no flag
  to skip review -- a flag fails open when forgotten. Running with no ``--allow``
  at all is a valid outcome: an empty-but-valid bundle (persona + tools, no
  private skills, no owner MCP servers), so the failure direction is
  under-sharing.
* **The content pin.** Every reviewed entry records the sha256 of the content it
  was written from, and the build re-checks that hash for each SELECTED entry. A
  skill or server edited after approval refuses the build and is named.
  Yesterday's approval cannot be laundered across today's content.

INTERFACE (PACKAGING-CONTRACT.md T1)
------------------------------------
    python -m packaging.build --crew <name> --out <dir> [--allow <path>]...
    python -m packaging.build plan  --crew <name> --out <dir> [--allow <path>]...

``build`` (the default verb) writes the four-entry layout into ``<dir>`` and
prints, as the LAST line, ``SMC_BUNDLE_JSON=<path>`` naming a JSON file with
``crew_name``, ``bundle_dir``, ``digest``, ``skill_count``, ``mcp_servers`` and
``denied``. ``plan`` prints the same decision set and writes a fresh
deny-by-default review template, WITHOUT writing a bundle.

``--crew`` names the crew; its source is a "crew home" holding
``agents/<name>.json`` and ``skills/``. ``--source`` overrides that root (a test
points it at a fixture); by default the agent spec resolves under
``$KIRO_HOME`` / ``~/.kiro`` and skills under ``$KIROCREW_HOME`` -- the same
locations Kiro Crew uses (``kiro_crew/config/paths.py:604`` ``kiro_agents_dir`` =
``kiro_home()/agents``, ``:510`` ``kiro_home``; ``config_dir()/skills`` per
``crew_export/candidates.py``). Never defaults to a temp dir.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import IO

# The frozen layout the image copies in and the container reader validates.
BUNDLE_VERSION = 1
PLAN_VERSION = 1

#: Identifies a report THIS tool wrote. Its only job is origin: the report path is derived
#: from --out, in a directory the build does not own, so replacing an existing file there
#: needs proof rather than a matching name. Same role ``PLAN_VERSION`` plays for the plan.
REPORT_VERSION = 1
PLAN_FILENAME = "curation-plan.json"

#: Every top-level name ``build_bundle`` writes inside its staging directory. A
#: staging path holding anything else is refused rather than deleted -- see the
#: check in ``build_bundle``. Kept beside ``PLAN_FILENAME`` because the plan is one
#: of them (it is carried across the swap).
_STAGING_OWNED_TOP_LEVEL: frozenset[str] = frozenset(
    {"agent.json", "mcp.json", "manifest.json", "skills", PLAN_FILENAME}
)

#: The only directory this build creates and may legitimately leave EMPTY.
#:
#: The empty-directory check exempted all of ``_STAGING_OWNED_TOP_LEVEL``, and four of those
#: five entries are FILE names -- so an operator's own empty directory called ``agent.json`` or
#: ``manifest.json`` was exempted and then removed by the recursive delete. The two sets overlap
#: because both describe what this build writes at the top level; what differs is that only one
#: of them can have nothing inside it.
_BUILD_WRITES_EMPTY: frozenset[str] = frozenset({"skills"})


def _is_shape_this_build_never_writes(p: "Path") -> bool:
    """True for anything that is not a plain file or a plain directory.

    Both replacement checks in ``build_bundle`` decided ownership with ``p.is_file()``,
    which is False for an empty directory, a FIFO, a socket, a device node and a link
    to a directory. Every one of those therefore passed the scan that exists to refuse
    unowned content, and was then deleted by the ``shutil.rmtree`` that follows.
    Measured before this existed: an empty directory and a FIFO both survived the scan
    and were removed.

    A symlink is judged BEFORE ``is_file()``, which follows links. This build writes
    plain files and directories only, so a link is a shape it never produced no matter
    what its target looks like or what the entry is called.
    """
    if _is_redirecting_entry(p):
        # ``is_symlink()`` was the test here and it is too narrow: a Windows JUNCTION is a
        # reparse point that is not reported as a symlink, and ``shutil.rmtree`` traverses one
        # on Windows rather than unlinking it as it does a symlink. So a junction planted
        # inside the output directory turned the recursive delete loose on its target.
        return True
    return not p.is_file() and not p.is_dir()


# MCP servers Kiro Crew resolves to an absolute path to a local binary; copying
# the definition ships a path that does not exist in the container. Ported from
# ``crew_export/candidates.py:_CONTAINER_OWNED_MCP``.
_CONTAINER_OWNED_MCP = frozenset(
    {"kirocrew-core", "kirocrew-cron", "kirocrew-computer", "kirocrew-dashboard"}
)

# `@builtin` names kiro-cli's own native tool group, not an MCP server, so a
# tool reference to it is never treated as dangling. Ported from
# ``serving/smc/bundle.py:BUILTIN_TOOL_GROUPS``.
_BUILTIN_TOOL_GROUPS = frozenset({"builtin"})

# Spec keys dropped on export. Ported from ``crew_export/spec.py:_DROPPED_KEYS``:
# an inherited security posture or a file outside the bundle is a silent policy
# change in the deployment.
_DROPPED_SPEC_KEYS = ("hooks", "includeMcpJson")


# ---------------------------------------------------------------------------
# Failure mode: refusal only. Ported from ``crew_export/errors.py``.
# ---------------------------------------------------------------------------
class ExportRefused(RuntimeError):
    """The export cannot proceed and no bundle was written.

    A warning the operator can scroll past is not a control, so every guard
    aborts rather than degrading -- the alternative is shipping a bundle wrong in
    the one direction that matters.
    """


# ===========================================================================
# Credential scanning -- refuse, never warn.
#
# Ported in INTENT from ``crew_export/scan.py``, which delegates to
# ``kiro_crew.deploy.scan`` for the canonical pattern set. That module is NOT
# importable in this venv, so the hard-credential patterns below are a
# self-contained subset. This is a real narrowing versus the source and is
# called out in the track report: a credential shape the canonical set knows and
# this subset does not would pass. The credential-NAME gate is ported verbatim.
# ===========================================================================
# The AWS key-ID prefix group is taken from ``kiro_crew.credential_patterns`` when
# that import works, because a second hand-written copy of it is exactly the drift a
# repo guard exists to catch (``test_no_module_spells_the_prefix_group_by_hand``).
# The literal fallback keeps this module runnable standalone, which is the property
# that lets it be exercised as ``python -m packaging.build`` from the crew directory
# alone -- so the fallback is the exception, not the normal path.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.credential_patterns import AWS_KEY_ID_PREFIXES as _AWS_KEY_PREFIXES
except Exception:  # pragma: no cover
    _AWS_KEY_PREFIXES = "AKIA|ASIA"

_HARD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key", re.compile(rf"\b(?:{_AWS_KEY_PREFIXES})[0-9A-Z]{{16}}\b")),
    # A LABELLED secret. The pattern above matches an AWS key ID, which has a
    # recognisable prefix; the secret access key is 40 characters of base64 with no
    # prefix at all, so nothing above can see it and `SecretAccessKey=<secret>` in a
    # prompt reached the deployed image. What makes it findable is the label, which is
    # how this repo's own detector finds it (`security.py:_HARD_CREDENTIAL_RE`,
    # described in security_posture.py as covering "labelled secret-access-key and
    # session-token forms"). Spelled here from that same shape, and the canonical
    # module is preferred over it below when importable.
    (
        "aws-secret-labelled",
        re.compile(
            r"(?:SecretAccessKey|aws_secret_access_key|SessionToken|aws_session_token)"
            r"[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+",
            re.IGNORECASE,
        ),
    ),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    # The same header after URL or form encoding, where the spaces have become ``+`` or
    # ``%20``. The shared detector spells its separator ``[\s+%]`` for exactly this, and
    # copying it as a literal space here left the encoded form unmatched -- measured against
    # ``_HARD_CREDENTIAL_RE``, ``BEGIN+RSA+PRIVATE+KEY`` was caught there and missed here.
    # A persona pasted out of a browser or a curl transcript arrives in that shape.
    (
        "private-key-encoded",
        re.compile(r"BEGIN[\s+%]+(?:RSA|DSA|EC|OPENSSH)[\s+%]+PRIVATE[\s+%]+KEY"),
    ),
    # An SSH PUBLIC key line. Not itself a secret, and that is not the test this scan
    # applies: the shared detector refuses these too, because a key line in a bundled
    # persona means a keypair was pasted in and the private half is very likely beside it.
    # Missing from the local subset until a comparison against the shared patterns was run
    # rather than eyeballed.
    ("ssh-public-key", re.compile(r"\b(?:ssh-rsa|ssh-ed25519)[\s+%]")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    # The fine-grained PAT form, which the classic ``gh[pousr]_`` pattern above does not
    # match: ``github_pat_`` then a 22-char base62 id, an underscore, and a 59-char base62
    # secret. Standalone is the REAL scan path in the deployment venv, so a format the local
    # set misses ships unscanned there -- measured against the fine-grained token shape.
    ("github-fine-grained-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("vendor-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    # A JWT (three base64url segments split by dots, header starting ``eyJ``). Bearer tokens,
    # session tokens and signed credentials arrive in this shape pasted into a persona, and
    # the local set had no way to see one. The header segment is anchored on ``eyJ`` (``{"``
    # base64url-encoded) so an ordinary dotted identifier is not matched.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)

#: Sensitive locations, for the standalone case where ``kiro_crew.security`` is not
#: importable. Ported from ``security/paths.py:_SENSITIVE_HOME_DIRS``.
#:
#: This list exists because the alternative was worse. A fence conditional on an import is skipped
#: entirely when the import failed, on the reasoning that reading the agent spec is the
#: tool's whole purpose so refusing would make standalone mode unusable. That reasoning
#: holds for refusing, and does not hold for skipping: it made the fence conditional on
#: an import, so standalone mode was the ONE mode where a sensitive --source was read
#: and bundled. A second, coarser list is the same trade the credential scanner already
#: makes above, and it is checked in ADDITION to the shared question, never instead of it.
#: Note what is NOT here: ``.kiro/agents``. Upstream lists it under
#: ``_WRITE_PROTECTED_HOME_PATHS``, not the read-sensitive set, because the protection is
#: against WRITING a spec whose ``mcpServers.<name>.command`` the gateway would then exec.
#: ``is_sensitive_path("~/.kiro/agents/frontdesk.json")`` returns False, and this build only
#: reads. Including it made every run without ``--source`` refuse its own crew, since
#: ``~/.kiro`` IS the default source and the spec lives at ``~/.kiro/agents/<name>.json``.
#: The local list must never be STRICTER than the shared validator; a test pins that. That is
#: why ``.codex/auth.json`` is the token LEAF and not the ``.codex`` directory: the shared
#: validator classifies the leaf and returns False for the sibling config, so fencing the
#: whole directory here would refuse a path the rest of the tree reads.
_SENSITIVE_RELATIVE_DIRS = (
    ".aws",
    ".azure",
    ".claude/.credentials.json",
    ".codex/auth.json",
    ".config/gcloud",
    ".docker/config.json",
    ".git-credentials",
    ".gnupg",
    ".gpg",
    ".kiro/crew-auth-staging",
    ".kube/config",
    ".local/share/amazon-q",
    ".local/share/kiro-cli",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".ssh",
)


def _looks_sensitive_standalone(path_posix: str) -> bool:
    """Coarse fence for the standalone case: does any COMPONENT name a credential store?

    Component-wise rather than substring, so ``~/projects/sshconfig-notes`` is not caught
    by ``.ssh`` and ``~/.ssh/id_rsa`` is. Two-part entries are matched as consecutive
    components for the same reason.

    Deliberately coarser than the real predicate, which also resolves links. It
    is a floor for a mode that had NO floor, not a replacement -- when the shared
    validator is importable, both run.
    """
    # ``path_posix`` is already POSIX-form (the caller passes ``.as_posix()``), so its
    # components are parsed with ``PurePosixPath`` rather than a raw ``"/"`` split: same
    # result, and it reads as POSIX-string parsing rather than OS-path assembly (the
    # cross-platform gate flags a bare ``split("/")`` as the latter).
    parts = [part for part in PurePosixPath(path_posix).parts if part not in ("", ".", "/")]
    # ``casefold()``, not ``lower()``. Windows paths are case-insensitive, so ``~/.AWS``
    # names the same directory as ``~/.aws`` and must be caught; and casefold is what the
    # shared validator uses (``security/paths.py`` casefolds every anchored entry), so
    # ``lower()`` here would be a SECOND, weaker rule for the same question. The two
    # differ on real input: Turkish dotless i and the German sharp s both fold to forms
    # ``lower()`` leaves alone.
    folded = [part.casefold() for part in parts]
    for entry in _SENSITIVE_RELATIVE_DIRS:
        wanted = list(PurePosixPath(entry.casefold()).parts)
        span = len(wanted)
        for start in range(len(folded) - span + 1):
            if folded[start : start + span] == wanted:
                return True
    # The dir list above catches a credential STORE by directory (``~/.ssh/id_rsa``); it does
    # not catch a credential FILE by name in an ordinary directory (``~/.kiro/crew/.env``).
    # ``.env`` is the keystone leaf the security model protects, and the shared validator
    # catches it by name -- so the floor must too, or a standalone ``--allow`` of it reads
    # open whenever the shared validator is unavailable. Apply the same credential-name rule
    # the scan uses, to the final component.
    #
    # EXCEPT the crew spec leaf ``agents/<name>.json``. That basename is the operator's crew
    # name, which they choose freely -- a crew named ``credentials`` or ``client_secret`` is
    # legitimate, and its spec is not a credential file. The shared validator agrees: it
    # returns False for ``~/.kiro/agents/<name>.json`` because that is where the spec lives.
    # Applying the credential-name rule to that leaf would make the floor STRICTER than the
    # validator and refuse a legitimately named crew's own spec, so the leaf directly under an
    # ``agents`` directory ending ``.json`` is exempt from the name rule (the directory rules
    # above still run, and a non-``.json`` credential leaf under ``agents`` is still caught).
    if parts and _CREDENTIAL_NAME_RE.match(parts[-1]):
        is_crew_spec_leaf = (
            len(parts) >= 2 and folded[-2] == "agents" and parts[-1].casefold().endswith(".json")
        )
        if not is_crew_spec_leaf:
            return True
    return False


# Filenames that are credential stores by convention, matched before any read.
# Ported verbatim from ``crew_export/scan.py:_CREDENTIAL_NAME_RE``.
_CREDENTIAL_NAME_RE = re.compile(r"""(?ix)
    ^(
        \.env(\..*)?
      | .*\.pem
      | .*\.p12
      | .*\.pfx
      | .*\.key
      | id_(rsa|dsa|ecdsa|ed25519)(\.pub)?
      | \.npmrc
      | \.netrc
      | \.pgpass
      | \.pypirc
      | \.git-credentials
      | credentials(\.json)?
      | client_secret.*\.json
      | service[-_]account.*\.json
      | .*\.kdbx
      | \.htpasswd
    )$
    """)


@dataclass(frozen=True)
class Leak:
    origin: str
    kind: str
    line: int
    snippet: str

    def render(self) -> str:
        return f"{self.origin}:{self.line}: {self.kind}: {self.snippet}"


def refused_by_name(path: Path) -> bool:
    """True when a path is a credential store by its name alone.

    A ``.pem`` that happens not to match a content regex is still a private key,
    so the name is judged before the bytes are read.
    """
    return bool(_CREDENTIAL_NAME_RE.match(path.name))


# Credential DIRECTORIES denied as a path component at any depth. Mirrored from
# ``kiro_crew.security.DENIED_ROOT_PARTS`` (security.py:8254), which denies these
# names "at any depth and covers those two dirs [``.kube``/``.docker``] whole" --
# a superset of the ``.kube/config`` and ``.docker/config.json`` leaves pinned in
# ``_SENSITIVE_HOME_DIRS``. It is MIRRORED rather than imported on purpose:
# importing ``kiro_crew.security`` here would drag in ``kiro_crew.executors``,
# ``kiro_crew.sel`` and more, none of which are importable in this app's
# deployment venv (boto3 / fastapi / pydantic / pytest only -- see the module
# docstring and the ``_HARD_PATTERNS`` note). So the guard would pass in a dev
# venv and fail at real packaging time, or pull the whole framework into the
# packager. This is a five-name set, not a large denylist, which is the
# narrowest-equivalent the track brief asks for.
#: Stored CASEFOLDED, because the membership test below folds each component before
#: comparing. Windows paths are case-insensitive, so ``~/.AWS/credentials`` names the same
#: file as ``~/.aws/credentials`` and a set of lowercase literals compared against raw
#: components misses it. The second such predicate in this module; the other one folds
#: too, and they must not disagree about what counts as a credential directory.
_CREDENTIAL_DIR_PARTS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})


def refused_by_location(path: Path) -> bool:
    """True when a path lies inside a known credential directory.

    ``refused_by_name`` catches a store named like one (``id_rsa``, ``*.pem``); it
    does NOT catch ``~/.kube/config``, whose basename ``config`` is innocent. A
    kubeconfig's ``client-certificate-data`` is base64 and may match no credential
    pattern, so the ``scan_text`` after the read cannot be relied on to catch it --
    and reading a file the repo already fences off is the wrong shape regardless
    of what the scanner would then find. Judge the location before the read.
    """
    return any(part.casefold() in _CREDENTIAL_DIR_PARTS for part in path.parts)


#: The repository's own hard-credential detector, when this module can reach it. The
#: local ``_HARD_PATTERNS`` above is a self-contained SUBSET and was documented as a
#: real narrowing; a review then found the exact gap that narrowing left (a labelled
#: AWS secret access key). So prefer the canonical one and keep the subset as the
#: fallback that lets this module run without ``kiro_crew`` installed -- the same
#: bargain ``_AWS_KEY_PREFIXES`` strikes, for the same reason.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.security import _HARD_CREDENTIAL_RE

    _CANONICAL_CREDENTIAL_RE: re.Pattern[str] | None = _HARD_CREDENTIAL_RE
except Exception:  # pragma: no cover
    _CANONICAL_CREDENTIAL_RE = None

#: The repo's redactor, imported for its ENCODED-credential detection. The patterns above
#: all match a credential written literally, so a base64 of the same bytes matched none of
#: them. This one decodes base64 chunks, and its warning list is what ``scan_text`` reads;
#: the redacted text is discarded, because this module refuses rather than edits.
#:
#: Imported rather than restated for the reason the canonical pattern is: a local subset
#: needs a new entry per shape, which does not converge.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.security import redact_credentials

    _CANONICAL_REDACTOR: Callable[[str], tuple[str, list[str]]] | None = redact_credentials
except Exception:  # pragma: no cover
    _CANONICAL_REDACTOR = None


_BARE_SECRET_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}(?![A-Za-z0-9+/])")
_BARE_SECRET_LEN = 40
_BARE_SECRET_ENTROPY_MIN = 4.3
_BARE_SECRET_MAX_LOWER_RUN = 5
_BARE_SECRET_MAX_VOWEL_RATIO = 0.30
_BARE_SECRET_HEX_ONLY_RE = re.compile(r"\A[0-9a-fA-F]+\Z")
_BARE_SECRET_VOWELS = frozenset("aeiouAEIOU")


def _bare_secret_decodes_to_printable(token: str) -> bool:
    """A base64 run whose decode is printable text is an encoded blob, not a bare key."""
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
    except Exception:
        return False
    if not raw:
        return False
    printable = sum(1 for b in raw if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D))
    return printable / len(raw) >= 0.85


def _bare_secret_window_is_key(token: str) -> bool:
    """One 40-char window has the shape of a bare AWS secret access key.

    A faithful, self-contained mirror of the canonical structural classifier, so the
    standalone deployment-path scan is not strictly weaker than the canonical one for this
    known shape. Every gate must pass, and the bias is toward NOT flagging: a false negative
    reverts to prior behaviour, a false positive refuses a benign build. Gates: exactly 40
    chars; all three of lower + upper + digit (rejects prose and all-one-class runs); not
    hex-only (a git sha or hex digest); no lowercase run over the cap (rejects dictionary-word
    identifiers and path segments); vowel ratio at or under the cap; Shannon entropy at or
    above the floor; and it does not base64-decode to printable text (an encoded blob is the
    decode pass's job, not this one).
    """
    if len(token) != _BARE_SECRET_LEN:
        return False
    if not (
        any(c.islower() for c in token)
        and any(c.isupper() for c in token)
        and any(c.isdigit() for c in token)
    ):
        return False
    if _BARE_SECRET_HEX_ONLY_RE.match(token):
        return False
    run = 0
    for ch in token:
        run = run + 1 if ch.islower() else 0
        if run > _BARE_SECRET_MAX_LOWER_RUN:
            return False
    letters = [ch for ch in token if ch.isalpha()]
    if letters and (
        sum(1 for ch in letters if ch in _BARE_SECRET_VOWELS) / len(letters)
        > _BARE_SECRET_MAX_VOWEL_RATIO
    ):
        return False
    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = -sum((c / len(token)) * math.log2(c / len(token)) for c in counts.values())
    if entropy < _BARE_SECRET_ENTROPY_MIN:
        return False
    return not _bare_secret_decodes_to_printable(token)


def _scan_bare_secret_runs(text: str, origin: str) -> list[Leak]:
    """Findings for a bare, unlabelled AWS secret access key in *text*.

    The canonical redactor catches this by shape in its bare-secret pass; the standalone path
    has only labelled patterns and a decode pass, and a bare 40-char secret carries no label
    and decodes to non-UTF-8 bytes, so without this it ships. A structural DETECTOR rather than
    a fourth literal pattern -- the shape that converges. A genuine 40-char key glued to
    adjacent base64 characters yields a 41+ char run, so a 40-char window is slid across each
    run (disjoint spans keep it linear); a run that decodes whole to printable text is a
    cohesive encoded blob and is left to the decode pass.
    """
    found: list[Leak] = []
    for match in _BARE_SECRET_RUN_RE.finditer(text):
        run = match.group(0)
        if _bare_secret_decodes_to_printable(run):
            continue
        for start in range(0, len(run) - _BARE_SECRET_LEN + 1):
            window = run[start : start + _BARE_SECRET_LEN]
            if _bare_secret_window_is_key(window):
                found.append(
                    Leak(
                        origin=origin,
                        kind="bare-secret",
                        line=0,
                        snippet=window[:4] + "…(%d chars)" % len(window),
                    )
                )
                break
    return found


def scan_text(text: str, origin: str) -> list[Leak]:
    """Hard credential findings in *text*. A finding aborts the build."""
    leaks: list[Leak] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _HARD_PATTERNS:
            m = pattern.search(line)
            if m:
                token = m.group(0)
                snippet = token[:4] + "…(%d chars)" % len(token)
                leaks.append(Leak(origin=origin, kind=kind, line=lineno, snippet=snippet))
        if _CANONICAL_CREDENTIAL_RE is not None:
            m = _CANONICAL_CREDENTIAL_RE.search(line)
            if m:
                token = m.group(0)
                leaks.append(
                    Leak(
                        origin=origin,
                        kind="repo-credential-detector",
                        line=lineno,
                        snippet=token[:4] + "…(%d chars)" % len(token),
                    )
                )
    # Encoded credentials, via the repo's OWN redactor rather than a fourth local pattern.
    #
    # ``_HARD_PATTERNS`` and the canonical detector both match a credential written
    # literally. A base64 of the same bytes matches neither, so a labelled secret survived
    # every scan and shipped -- and this module already knows that adding one more local
    # pattern per shape is what does not converge, which is why the prompt fence prefers
    # ``is_sensitive_path`` over its own list.
    #
    # ``redact_credentials`` decodes base64 chunks and reports what it found, so its WARNING
    # list is the signal here; the redacted text is discarded because this function refuses
    # rather than edits. Run over the whole text, not per line: an encoded blob can wrap.
    if _CANONICAL_REDACTOR is not None:
        try:
            _, warnings = _CANONICAL_REDACTOR(text)
        except Exception:  # a detector fault must not become a silent pass
            warnings = ["credential redactor raised; treating the content as unscannable"]
        for warning in warnings:
            leaks.append(Leak(origin=origin, kind="repo-redactor", line=0, snippet=warning[:80]))
    else:
        # The import failed, which is the documented standalone mode. Encoded detection must
        # not simply VANISH with it: a build that silently stops looking for a class of leak
        # is worse than one that never claimed to, because the plan's notes still say the
        # content was scanned.
        #
        # So the fallback DECODES rather than re-describing what a credential looks like. It
        # feeds ``_HARD_PATTERNS`` -- the same patterns the literal pass uses -- over the
        # decoded bytes. That is deliberately not a fourth local credential pattern: adding
        # one pattern per shape is the shape that does not converge, and a decoder
        # inherits every future pattern for free where a pattern list would not.
        leaks.extend(_scan_decoded_runs(text, origin))
        # The canonical redactor's bare-secret pass has no counterpart in the patterns above,
        # so a label-less 40-char AWS secret access key -- which matches no ``_HARD_PATTERNS``
        # entry and base64-decodes to non-UTF-8 bytes the decode pass skips -- would ship only
        # in this standalone mode. The structural detector closes that so the deployment-path
        # scan is not weaker than the canonical one for this shape.
        leaks.extend(_scan_bare_secret_runs(text, origin))
    return leaks


#: Base64 runs long enough to hide a credential. The floor is 20 characters, not 40: 40 is
#: the length of an AWS *secret access key* specifically, but ``_HARD_PATTERNS`` also matches
#: shorter secrets (a labelled ``aws_secret_access_key=<value>`` fragment, a vendor ``sk-``
#: key, a github/slack token) whose base64 run is well under 40 chars, and in the standalone
#: deployment venv this decoder is the REAL scan path (the canonical redactor is not
#: importable), not a rare fallback. 20 base64 chars decode to ~15 bytes -- long enough to
#: carry a short credential, short enough that a bare word is not decoded as one.
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")

#: Ceiling on how much of one text is decoded, so a large file cannot turn the scan into the
#: build's slowest step. Runs are examined longest-first, because a credential plus its label
#: is longer than a bare token and the long runs are the ones worth the budget.
_B64_DECODE_BUDGET = 256 * 1024


def _scan_decoded_runs(text: str, origin: str) -> list[Leak]:
    """Findings from base64 runs in *text*, judged by the same patterns as the literal pass.

    Not recursive: one decode. A credential wrapped twice is out of scope here and stays with
    the canonical redactor, which is preferred whenever it can be imported.
    """
    found: list[Leak] = []
    spent = 0
    skipped_unscanned = 0
    # Longest first, because a credential plus its label is longer than a bare token, so the
    # long runs are the ones worth the budget.
    #
    # ``continue`` and NOT ``break``. This was ``break``, and combined with that ordering it
    # made a single oversized run disable the scan completely: the longest run is examined
    # first, so if it alone exceeded the budget the loop exited before reading anything, and
    # every shorter run -- including the one carrying the credential -- went unscanned. A
    # blob big enough to trip the ceiling is trivially easy to include, which turned a memory
    # bound into an off switch.
    for match in sorted(_B64_RUN_RE.finditer(text), key=lambda m: -len(m.group(0))):
        run = match.group(0)
        if spent + len(run) > _B64_DECODE_BUDGET:
            # FAIL CLOSED. ``continue`` alone was still a silent pass: a credential inside a
            # run past the budget went unscanned and the output said the content was clean.
            # ``break`` was worse (one oversized run disabled everything) but both shared the
            # same flaw -- unscanned reported as scanned. A Leak is appended instead, so the
            # build refuses and names what it could not read.
            skipped_unscanned += 1
            continue
        spent += len(run)
        try:
            raw = base64.b64decode(run + "=" * (-len(run) % 4), validate=True)
            decoded = raw.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError):
            # Not base64, or not text once decoded. Either way there is nothing here that the
            # literal patterns could read, so it is not a finding.
            continue
        for kind, pattern in _HARD_PATTERNS:
            hit = pattern.search(decoded)
            if hit:
                token = hit.group(0)
                found.append(
                    Leak(
                        origin=origin,
                        kind=f"encoded-{kind}",
                        line=text.count("\n", 0, match.start()) + 1,
                        snippet=token[:4] + "…(%d chars, base64)" % len(token),
                    )
                )
    if skipped_unscanned:
        found.append(
            Leak(
                origin=origin,
                kind="unscannable-encoded",
                line=0,
                snippet=(
                    "%d base64 run(s) past the %d byte decode budget were NOT scanned"
                    % (skipped_unscanned, _B64_DECODE_BUDGET)
                ),
            )
        )
    return found


# ===========================================================================
# Candidate enumeration -- everything starts excluded.
# Ported from ``crew_export/candidates.py`` (skills + mcp only: the app's
# four-entry layout has no workspace/ or knowledge/, so those categories, and
# the sqlite knowledge walk behind them, are deliberately not ported).
# ===========================================================================
@dataclass
class Candidate:
    kind: str  # "skills" | "mcp"
    id: str
    #: sha256 of the candidate's content; the pin the review records and the
    #: build re-checks. Empty only for a blocked candidate that was never read.
    content_hash: str
    note: str = ""
    #: Set when structurally ineligible (a credential store); refused if selected.
    blocked: str = ""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _staged_tree_hash(staged_dir: Path, source_dir: Path, written: "set[str]") -> str:
    """``_tree_hash`` of the staged copy, restated in the SOURCE's terms.

    The pin was taken by ``_tree_hash`` over every file in the source. The copy does
    not ship every file: ``_copy_skill`` drops binary assets, because a file it cannot
    decode is a file it cannot scan. So hashing the staged directory alone can never
    equal the pin for a skill carrying an image, and comparing them directly would
    refuse a legitimate skill -- which is what the first version of this check did.

    So the rows are built from the staged bytes where a file shipped, and from the
    SOURCE bytes only for the files the copy deliberately dropped. The security
    property is preserved where it matters: every file whose bytes reach the bundle is
    hashed from the copy that reaches it, so a mid-copy rewrite of a shipped file
    changes this value. A rewrite of a DROPPED file is not covered, and cannot matter,
    because those bytes are not in the artifact.

    A path that exists in STAGING but not in the source ships bytes no reviewer approved.
    The verification set is therefore derived from what will actually ship: after the
    source-keyed rows, every staged file with no source counterpart contributes its own
    row, so a staged-only injection changes this value and the caller's pin comparison
    refuses it. This does not break the equality the pin needs, because a legitimate copy
    is a SUBSET of the source (``_copy_skill`` only ever writes source-derived files and
    drops some) -- so a clean build produces zero staged-only rows and still equals
    ``_tree_hash(source)``. The intentional omissions run the other way (source files the
    copy dropped), and those are covered by the source-keyed rows above, not here.
    """

    rows: list[list[str]] = []
    source_rels: set[str] = set()
    for p in _walk_no_reparse(source_dir):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(source_dir).as_posix()
        source_rels.add(rel)
        shipped = staged_dir / rel
        if _is_redirecting_entry(shipped):
            # The write is no-follow, but the READ here is a separate window: a staged leaf
            # swapped to a symlink after it was written would be hashed THROUGH the link
            # (``is_file``/``read_bytes`` both follow), pinning the link target's bytes as the
            # reviewed content while a different object ships. Reject the redirect at final
            # hashing so the pin is taken over the object that was written, not one substituted
            # under its name.
            raise ExportRefused(
                f"the staged file {rel} is a link or junction at hashing time; it was "
                f"redirected after this build wrote it. Refusing rather than pin the bytes of "
                f"whatever it now points at. Re-run the build."
            )
        if shipped.is_file():
            rows.append([rel, _sha(shipped.read_bytes())])
        elif rel in written:
            # ``_copy_skill`` WROTE this file, and it is gone from staging now -- removed or
            # replaced between the write and this read-back. That is a torn staged tree, not a
            # reviewed state, so it is REFUSED. Falling back to the source bytes here (which is
            # correct only for a file the copy never wrote) would hash what SHOULD have shipped
            # rather than what did, counting the disappearance as reviewed. "I wrote it" is a
            # cached assumption with a window under it.
            raise ExportRefused(
                f"the staged file {rel} was written by this build and is now missing from the "
                f"staged tree; it changed after it was written. Refusing rather than count the "
                f"absence as reviewed. Re-run the build."
            )
        else:
            # A source file the copy legitimately did NOT stage -- it belongs to an unselected
            # nested skill. The pin (``_tree_hash`` over the whole source) still covers it, so
            # its source bytes keep the equality; its bytes are not in the artifact, so a source
            # change to it cannot matter. This is the ONLY legitimate not-staged case now that
            # ``_copy_skill`` refuses (never silently drops) an unscannable file.
            rows.append([rel, _sha(p.read_bytes())])
    # Staged-only files: present in what ships, absent from the reviewed source. A clean
    # copy has none (staging is a subset of source), so this adds nothing to a legitimate
    # build's hash and the pin equality holds; an added-then-removed mid-copy file leaves a
    # staged path with no source row, which lands here and breaks the equality so the build
    # refuses. Judged by ``lstat`` shape like the source walk (a link or junction is not a
    # shipped regular file and its target is out of the artifact).
    for p in _walk_no_reparse(staged_dir):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(staged_dir).as_posix()
        if rel not in source_rels:
            rows.append(["staged-only:" + rel, _sha(p.read_bytes())])
    return _sha(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _tree_hash(root: Path) -> str:
    """A content hash over every file in a directory, path-and-content, sorted.

    Any byte or any filename changing changes the hash -- the property the
    content pin needs. Modelled on ``crew_export/candidates.py``'s skill
    ``tree_hash``, widened to hash every file rather than only ``SKILL.md`` so an
    edit to any file in the skill invalidates approval.
    """
    rows: list[list[str]] = []
    for p in _walk_no_reparse(root):
        # ``is_symlink()`` misses a junction, which ``rglob`` descends into: hashing a file
        # under a junction would fold bytes from outside ``root`` into the tree hash. Skip any
        # file reached through a redirecting component so the hash covers only in-tree content.
        if p.is_file() and not p.is_symlink() and _redirect_between(root, p) is None:
            rows.append([p.relative_to(root).as_posix(), _sha(p.read_bytes())])
    return _sha(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


#: Extra ``os.open`` flags for reading a file that must not be a symlink, guarded
#: because NEITHER constant exists on every platform. ``O_NOFOLLOW`` is the security
#: half (refuse a final-component link at open time) and ``O_NONBLOCK`` is the
#: liveness half (a FIFO would otherwise block the open forever, before any check
#: runs). Windows has neither, and getattr'ing only one of them is precisely the bug
#: that reddened five tests on the Windows shard: two platform-specific constants on
#: one line, one of them guarded.
_NOFOLLOW_READ_FLAGS: int = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _read_text(path: Path) -> str | None:
    # newline="" on the READ for the same reason _write_guarded pins it on the write, and
    # the two only work as a pair. The default (newline=None) is universal-newlines
    # DECODING: it turns a CRLF file into a string holding "\n". Pinning only the write
    # therefore moved the corruption rather than removing it -- a CRLF-authored skill was
    # read as LF and staged as LF while ``_tree_hash`` had pinned the CRLF source, so the
    # build refused with "changed while the bundle was being written" exactly as it did
    # before, in the opposite direction.
    #
    # With both ends pinned the round trip is byte-preserving whatever the file holds,
    # which is the property the content pin actually needs: what ships is what was
    # hashed. It is not "normalise to LF" -- normalising would require re-hashing the
    # source through the same transform, and a builder that rewrites an operator's bytes
    # is a worse thing than one that carries them.
    # ``open`` rather than ``read_text(newline="")``: pathlib's reader only grew that
    # keyword in 3.13, while ``write_text`` has had it since 3.10, so the pair has to be
    # spelled asymmetrically to work on the versions this package supports.
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except (UnicodeDecodeError, OSError):
        return None


def _read_text_nofollow(path: Path) -> str | None:
    """Read *path* as UTF-8, refusing a symlink at the OPEN, not before it.

    ``_read_text`` opens through ``pathlib``, which follows a final-component link,
    so a caller that first checks ``is_file()`` and then reads has a check/read
    window: a concurrent writer with access to the source tree can loop-swap the
    file for a symlink between the two and be read through. Opening with
    ``O_NOFOLLOW`` collapses the check and the read into one syscall -- there is no
    moment between them to win -- so the link is refused by the kernel at open time
    rather than by a separate stat that the read then races. Returns ``None`` on a
    link, a FIFO (``O_NONBLOCK`` keeps the open from hanging), a non-UTF-8 body, or
    any other open error, exactly like ``_read_text``.
    """
    # No-follow read on BOTH platforms. On POSIX, ``O_NOFOLLOW`` refuses a final-component
    # link atomically at the open. On Windows ``O_NOFOLLOW`` is ``0`` (``getattr`` default),
    # so the open would follow a reparse point -- a junction to a UNC path is then an outbound
    # SMB/NTLM probe. There is no atomic no-follow open there, so fail CLOSED: ``lstat`` the
    # path first and refuse a reparse point (``_is_redirecting_entry`` sees a junction, which
    # ``is_symlink`` does not) before opening. A residual check-then-open window remains on the
    # platform with no atomic primitive, but a planted or already-swapped reparse point is
    # refused rather than followed -- the same posture ``_read_text_openat`` takes.
    if not getattr(os, "O_NOFOLLOW", 0) and _is_redirecting_entry(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW_READ_FLAGS)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except (UnicodeDecodeError, OSError):
        return None


def _read_text_openat(root: Path, rel: Path) -> str | None:
    """Read ``root/rel`` as UTF-8, refusing a redirect at EVERY component, not only the last.

    ``_read_text_nofollow`` collapses check and read into one ``O_NOFOLLOW`` open, but
    ``O_NOFOLLOW`` guards only the FINAL component. An intermediate directory on the path
    (``agents/`` on the way to ``agents/frontdesk.json``) swapped for a junction or symlink
    AFTER a separate chain check and BEFORE the open is a check/open TOCTOU a concurrent
    writer can win. This walks ``rel`` one component at a time from ``root``, opening each
    directory with ``O_NOFOLLOW | O_DIRECTORY`` relative to the previous one's descriptor
    (``openat`` semantics), so a component swapped for a redirect fails its OWN open -- there
    is no path string re-resolved after a check. The final component is opened ``O_RDONLY |
    O_NOFOLLOW`` relative to the last directory fd.

    Falls back to ``_read_text_nofollow`` where ``dir_fd`` is unsupported (Windows), the same
    trade the rest of this module makes; there the final-component ``O_NOFOLLOW`` still holds
    and only the intermediate anchoring is lost, on the platform whose links differ anyway.
    Returns ``None`` on any redirect, missing component, special file, or non-UTF-8 body.
    """
    parts = rel.parts
    if not parts:
        return None
    if not _dir_fd_supported():
        # Windows has neither ``dir_fd`` nor a working ``O_NOFOLLOW`` (it is ``0`` here), so
        # the openat walk below is unavailable and ``_read_text_nofollow`` alone would guard
        # nothing -- an intermediate junction swapped under a component would be followed into
        # an untrusted file. Fail closed instead of best-effort: ``lstat`` every component
        # from ``root`` down and refuse if ANY is a reparse point (a junction is not a symlink,
        # so ``_is_redirecting_entry`` is the check, not ``is_symlink``). A residual
        # check-then-read window remains on this platform -- there is no atomic no-follow open
        # to close it -- but a planted or swapped-before-the-walk redirect is refused rather
        # than traversed, which is the fail-closed posture the openat path gives elsewhere.
        if _redirect_between(root, root / rel) is not None:
            return None
        return _read_text_nofollow(root / rel)
    file_fd = _open_leaf_nofollow_at(root, rel)
    if file_fd is None:
        return None
    try:
        with os.fdopen(file_fd, "r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except (UnicodeDecodeError, OSError):
        return None


def _open_leaf_nofollow_at(root: Path, rel: Path) -> "int | None":
    """Open ``root/rel`` for reading, pinning EVERY component no-follow; return the leaf fd.

    Walks ``rel`` one component at a time from ``root``, opening each directory with
    ``O_NOFOLLOW | O_DIRECTORY`` relative to the previous descriptor and the final component
    ``O_RDONLY | O_NOFOLLOW`` relative to the last -- so a component swapped for a redirect
    fails its own open with no path string re-resolved after a check. The caller owns the
    returned fd and must close it (the text/bytes readers below wrap it in ``fdopen``).
    Returns ``None`` on any redirect, missing component, or non-directory intermediate.
    """
    parts = rel.parts
    if not parts:
        return None
    if not _dir_fd_supported():
        # Unreachable via the readers (they take the Windows fallback before calling here), but
        # stated locally so the rule that every ``O_DIRECTORY`` user consults ``_dir_fd_supported``
        # holds by reading -- without it this would raise ``AttributeError`` on ``O_DIRECTORY``.
        return None
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        # The root gets the SAME dir_flags as every component below it. Opening it
        # without O_NOFOLLOW made the anchor itself the hole: a link swapped in at
        # ``root`` was followed, and the walk then correctly refused redirects
        # *inside* a tree that was already the wrong tree.
        cur_fd = os.open(str(root), dir_flags)
    except OSError:
        return None
    open_dirs = [cur_fd]
    try:
        for part in parts[:-1]:
            cur_fd = os.open(part, dir_flags, dir_fd=cur_fd)
            open_dirs.append(cur_fd)
        try:
            return os.open(parts[-1], os.O_RDONLY | _NOFOLLOW_READ_FLAGS, dir_fd=cur_fd)
        except OSError:
            return None
    except OSError:
        # A redirect (ELOOP), a missing or non-directory component: none is a file to read.
        return None
    finally:
        for d in open_dirs:
            os.close(d)


def _read_bytes_openat(root: Path, rel: Path) -> "bytes | None":
    """Read ``root/rel`` as RAW BYTES, refusing a redirect at EVERY component.

    The bytes counterpart of :func:`_read_text_openat`, for a caller that needs the exact
    bytes (a signed plan carried verbatim, the report drift baseline) rather than decoded
    text. Same whole-window no-follow walk; falls back to a leaf-only no-follow read where
    ``dir_fd`` is unsupported (Windows), after refusing a reparse point anywhere on the chain.
    Returns ``None`` on any redirect, missing component, or read error.
    """
    if not rel.parts:
        return None
    if not _dir_fd_supported():
        if _redirect_between(root, root / rel) is not None:
            return None
        try:
            fd = os.open(root / rel, os.O_RDONLY | _NOFOLLOW_READ_FLAGS)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "rb") as fh:
                return fh.read()
        except OSError:
            return None
    file_fd = _open_leaf_nofollow_at(root, rel)
    if file_fd is None:
        return None
    try:
        with os.fdopen(file_fd, "rb") as fh:
            return fh.read()
    except OSError:
        return None


#: First line of the staging marker. Its job is to tell OUR marker apart from any other
#: file that happens to sit at that path, because the previous check was
#: ``staging_marker.is_file()`` and every plain file satisfies that -- an operator's own
#: note beside their own ``<name>.staging`` directory authorised a recursive delete of it.
#:
#: What this is NOT: authentication. Anyone who can write to ``out_dir.parent`` can write
#: this line too. The threat it removes is COLLISION, which is the one that happens by
#: accident; against an adversary who already has write access to that directory a forged
#: marker is not the shortest path to harm, since they can delete the staging tree
#: themselves. Stated here rather than implied so nobody reads the token as a secret.
_STAGING_MARKER_TOKEN = "kiro-crew-bundle-staging-marker/1"

#: Identifies THIS run, not just this builder.
#:
#: The token alone said "a kiro-crew build made this", which two concurrent builds against the
#: same --out both satisfy -- so each read the other's marker as its own and deleted the other's
#: staging tree with the recursive delete the marker authorises. The loser then promoted a
#: half-built bundle or crashed on a missing file.
#:
#: pid plus randomness, because pid alone repeats: a container that reruns the builder can see
#: the same pid, and a stale marker from a killed run would then look like this run's own.
_RUN_ID = f"{os.getpid()}-{uuid.uuid4().hex[:16]}"

_STAGING_MARKER_BODY = (
    _STAGING_MARKER_TOKEN + "\n" + _RUN_ID + "\n"
    "Written by kiro-crew's crew bundle builder so a later run can tell this staging\n"
    "directory apart from one you created. Safe to delete when no build is running.\n"
)


def _dir_fd_supported() -> bool:
    """Whether a path can be pinned by opening its parent as a descriptor.

    One predicate for the three places that need it, because the answer must be the same
    in all of them: ``_open_nofollow_under`` asked it inline first, and the two functions
    added later did not ask at all, which turned every Windows build into an
    ``AttributeError`` on ``os.O_DIRECTORY`` before it did anything.

    False is Windows. It is a real narrowing of what those functions promise, spelled as a
    branch at each call site rather than hidden here, so a reader sees which guarantee is
    lost where.
    """
    return os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")


def _nofollow_primitive_available() -> bool:
    """Whether this platform gives the builder an atomic no-follow filesystem primitive.

    Every path this builder reads, stats, enumerates or mutates has to be judged without
    following a reparse point, because following one that names a UNC share is an outbound
    SMB probe carrying an NTLM exchange. On POSIX that primitive exists: descriptor-relative
    ``O_NOFOLLOW`` opens (``_dir_fd_supported`` plus a working ``os.O_NOFOLLOW``) refuse a
    reparse component atomically. On Windows ``os.O_NOFOLLOW`` is ``0`` and there is no
    descriptor-relative open, so the fallback for every entry point follows -- the guarantee
    is absent, not merely narrower.

    Feature-detected, NOT ``os.name == "nt"``: the day ``kiro_crew.hooks`` grows a real
    no-follow handle (a ``FILE_FLAG_OPEN_REPARSE_POINT`` open) and this builder adopts it,
    this predicate turns True on its own and the entry-point guard lifts without anyone
    remembering it exists. A bare platform check would strand the guard after the fix.
    """
    return _dir_fd_supported() and bool(getattr(os, "O_NOFOLLOW", 0))


def _refuse_without_nofollow_primitive() -> None:
    """Refuse at the entry point on a platform with no atomic no-follow primitive.

    One entry-point guard, because the alternative -- hardening each of the builder's ~15
    filesystem entry points against reparse-following on the Windows fallback branch -- is a
    site list, and a site list is complete only until the next one is found. The guarantee
    this builder needs (no read/stat/enumerate/mutate ever follows a reparse point to a share)
    is a property of the platform's primitives, so it is checked once where the primitive is
    absent rather than re-argued at every call. This is a deliberate hold with a tracked exit,
    not a bug: the builder is POSIX-only until the primitive lands.
    """
    if not _nofollow_primitive_available():
        raise ExportRefused(
            "the crew bundle builder is POSIX-only for now: this platform has no atomic "
            "no-follow filesystem primitive, so its filesystem entry points would follow a "
            "reparse point (a Windows junction to a UNC share) and leak an SMB/NTLM exchange "
            "during ordinary packaging. Refusing rather than ship that surface. Tracked in "
            "issue #9496; the guard lifts automatically when the primitive is available."
        )


def _is_redirecting_entry(probe: Path) -> bool:
    """Whether *probe* redirects to somewhere else: a symlink, or any reparse point.

    ``is_symlink()`` alone is the wrong question on Windows. A JUNCTION is a reparse point
    that is NOT reported as a symlink, and a junction is precisely what gets planted over a
    directory to redirect it, so a symlink-only check would pass the attack through. The
    attribute is read from the ``lstat`` result so the entry itself is inspected rather than
    its target.

    A missing entry is not redirecting: the caller's own open reports it, with the error
    message that fits where it happened.
    """
    try:
        st = os.lstat(probe)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _redirect_between(root: Path, path: Path) -> Path | None:
    """The first redirecting component on ``root -> path``, or ``None`` if the walk is clean.

    ``rglob`` and ``is_symlink()`` are not enough to keep a tree walk inside its root.
    ``rglob("*")`` DESCENDS into a directory junction (a non-symlink reparse point), and a
    file under that junction reports ``is_symlink()`` False, so it copies or hashes as an
    ordinary in-tree file even though its bytes live at the junction's target -- outside the
    crew source. Every ``rglob`` walk that trusts ``is_symlink()`` therefore needs this: it
    ``lstat``s each component below ``root`` with ``_is_redirecting_entry`` (which sees a
    junction, not only a symlink) and returns the first that redirects, so the caller can
    skip or refuse the file rather than ship someone else's bytes under a harmless name.

    ``path`` is assumed to be at or below ``root`` (it comes from ``root.rglob``). The
    components strictly between ``root`` and ``path`` are checked, then ``path`` itself.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        # Not under root -- treat the whole path as suspect rather than vouching for it.
        return path
    cur = root
    for part in rel.parts:
        cur = cur / part
        if _is_redirecting_entry(cur):
            return cur
    return None


def _walk_no_reparse(root: Path, *, match: str | None = None) -> "list[Path]":
    """Every descendant of ``root``, like ``root.rglob(match or '*')``, but NEVER descending
    a reparse point.

    ``pathlib.rglob`` walks a directory junction (a non-symlink reparse point) by
    construction, and on Windows walking a junction that names a UNC share is an outbound
    SMB/NTLM probe -- so the leak happens during ENUMERATION, before any post-hoc
    ``is_symlink`` / ``_redirect_between`` guard on the yielded path can refuse it. No amount
    of checking after the fact makes a traversal that already entered a junction safe. This
    walks with ``os.scandir`` and, at each directory, refuses to RECURSE into an entry that
    is a reparse point: the entry itself is still yielded (so a caller that wants to block or
    report it sees it), but its subtree is never entered, so the probe never fires. On a
    platform where ``scandir``/reparse detection is unavailable the result is identical to
    ``rglob`` for an ordinary tree; the reparse refusal is what Windows needs and POSIX
    ``scandir`` provides via ``is_symlink``.

    Returns a sorted list (callers relied on ``sorted(rglob(...))`` for a stable hash order).
    A missing directory yields nothing (a crew with no skills dir is the ordinary case); a
    directory that EXISTS but cannot be listed -- or whose entry cannot be stat'd to decide
    whether to descend -- fails closed with ``ExportRefused`` rather than reading as empty or
    as a leaf, so an unreadable selected directory cannot ship a silently incomplete bundle.
    """
    found: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except FileNotFoundError:
            # A missing directory is absence, not an unreadable selection: the ROOT being
            # absent is the ordinary "this crew has no skills dir" case and yields empty, like
            # ``rglob``; a subdirectory pushed while it existed and gone now lost a race with a
            # concurrent remove -- nothing there to ship, so skip it.
            continue
        except OSError as exc:
            # A directory that EXISTS but cannot be listed (a permission change, an I/O error)
            # must NOT read as "empty" -- that is how an unreadable selected-skill directory
            # shipped a silently incomplete signed bundle: enumeration, copy, and the pin
            # recheck all skipped it. Fail closed and name the directory. Absent / unreadable /
            # unscannable never counts as "not selected".
            raise ExportRefused(
                f"the directory {current} exists but could not be listed ({exc}); refusing "
                f"rather than ship a bundle that silently omits what is under it. Fix its "
                f"permissions or remove it."
            ) from exc
        for entry in entries:
            p = Path(entry.path)
            if match is None or entry.name == match:
                found.append(p)
            # Recurse only into a REAL directory, never a reparse point. ``follow_symlinks``
            # is False so ``is_dir`` answers about the link itself; ``_is_redirecting_entry``
            # additionally catches a Windows junction, which ``is_symlink`` does not.
            try:
                is_real_dir = entry.is_dir(follow_symlinks=False)
            except FileNotFoundError:
                # Lost a race with a concurrent remove between the scandir and this stat,
                # the same case the scandir arm above skips: there is nothing left to
                # descend into.
                is_real_dir = False
            except OSError as exc:
                # An entry that EXISTS but cannot be inspected must not read as "not a
                # directory". That is the silent omission this function refuses one level
                # up, arriving one level down: an unstattable directory is never pushed, so
                # its whole subtree leaves the walk, and the candidate list, the copy and
                # the hash are all computed over what remains. The bundle is then signed
                # while missing files nothing reported. Same verdict as an unlistable
                # directory, for the same reason.
                raise ExportRefused(
                    f"{p} exists but could not be inspected ({exc}), so whether it is a "
                    f"directory to descend into is unknown; refusing rather than ship a "
                    f"bundle that silently omits what is under it. Fix its permissions or "
                    f"remove it."
                ) from exc
            if is_real_dir and not _is_redirecting_entry(p):
                stack.append(p)
    found.sort()
    return found


def _refuse_redirects_in_chain(root: Path, target: str, *, what: str = "prompt file") -> None:
    """Refuse a redirect at any component of ``root/target``, without resolving it.

    Walked one component at a time and judged by ``lstat``, so nothing here follows a link.
    That is the requirement: this runs BEFORE ``resolve()`` precisely because resolve is the
    traversal, and on Windows traversing a reparse point that names a share is an outbound
    SMB probe carrying an NTLM exchange.

    ``..`` is refused rather than normalised. Normalising it here would mean deciding what
    the path means without touching the filesystem, and ``a/../b`` is not ``b`` when ``a`` is
    a link -- which is the whole class of bug this function exists inside. The containment
    check after ``resolve()`` still runs and still has the final word on where the path
    landed; this only removes the redirects that made the resolve itself dangerous.
    """
    parts = Path(target).parts
    if not parts:
        return
    cur = root
    if _is_redirecting_entry(cur):
        raise ExportRefused(
            f"the anchor directory {root} is a link or junction. The walk below it is what "
            f"keeps a redirect from being traversed, and a redirect at the anchor itself makes "
            f"every check below examine someone else's directory. Refusing to read the {what} "
            f"through it."
        )
    for part in parts:
        if part == "..":
            raise ExportRefused(
                f"the {what} path names a parent directory ({target!r}). Resolving that is only "
                f"meaningful once every component above it is known not to be a link, so it "
                f"is refused rather than normalised. Reference the persona by a path that "
                f"does not climb."
            )
        if part in (".", ""):
            continue
        cur = cur / part
        if _is_redirecting_entry(cur):
            raise ExportRefused(
                f"{cur} is a link or junction on the path to the {what}. Following it "
                f"is what resolving this path would do, and on Windows a redirect naming a "
                f"share is an outbound SMB probe before any check runs. Refusing."
            )


def _refuse_unless_our_report(path: Path, out_dir: Path) -> None:
    """Refuse a file at the report path unless this tool wrote it.

    Absent is fine: the ordinary first build. A directory or a link is left to
    ``_write_nofollow``, which judges shape and reports it precisely. What this adds is the
    one case shape cannot answer -- a plain file that happens to have this name -- because
    truncating it is indistinguishable from rebuilding until you look inside.

    The name alone is not proof, which is the same lesson the plan-only directory check
    learned: a file called ``curation-plan.json`` was deleted on its name until the check
    started reading ``plan_version``.
    """
    if _is_redirecting_entry(path):
        # Judged BEFORE ``is_file()``, which follows the link and on Windows follows a
        # reparse point naming a share -- the outbound SMB probe, from a path derived from
        # --out. ``_write_nofollow`` refuses the link afterwards, so returning here hands it
        # the decision instead of reaching the network to make one.
        return
    if not path.is_file():
        return
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        body = None
    # Both fields, not just the version. ``report_version`` is a generic key: any unrelated
    # JSON that happens to carry ``"report_version": 1`` was accepted as this tool's own
    # output and truncated. ``bundle_dir`` is the report's claim about WHICH bundle it
    # describes, and this build is about to write out_dir, so a report that names a different
    # destination is not the one this build would be replacing -- whoever wrote it is not us.
    if (
        isinstance(body, dict)
        and body.get("report_version") == REPORT_VERSION
        and body.get("bundle_dir") == str(out_dir)
    ):
        return
    raise ExportRefused(
        f"{path} already exists and this build did not write it (it does not carry "
        f"report_version {REPORT_VERSION} naming bundle_dir {out_dir}). The path is derived "
        f"from --out by appending "
        f"'.smc-bundle.json', and writing the report would replace its contents. Move it, "
        f"or point --out elsewhere."
    )


def _refuse_unc_out(out_dir: Path) -> None:
    """Refuse a UNC-shaped ``--out`` before any path derived from it is touched.

    A screen that must ``lstat`` its subject to judge it cannot be the outermost one on
    Windows, because the touch IS the probe: ``lstat`` on a ``\\\\host\\share`` path reaches
    that host over SMB and carries an NTLM exchange before any check has run. So the purely
    local shape test -- read off the string, reaching nothing -- runs FIRST, and only a path
    that survives it earns a filesystem question. ``--out`` is author-supplied, the same class
    as the agent-spec and plan paths that already gate this way; every path this build touches
    (``out_dir``, its parent, the staging tree, the marker, the report) is derived from it, so
    the first ``_is_redirecting_entry`` or ``_refuse_unusable_parent`` below would otherwise be
    the probe. Guarded here rather than only at the CLI so the API surface is covered too.
    """
    if os.name != "nt":
        return
    try:
        from kiro_crew.hooks import is_unc_shape, unc_probe_allowed
    except ImportError as exc:
        raise ExportRefused(
            f"cannot judge whether --out {out_dir} names a UNC path, because "
            f"kiro_crew.hooks is not importable here ({exc}). Building there could reach a "
            f"host over SMB before any check runs, so it is refused rather than touched "
            f"unchecked. Point --out at a local directory."
        ) from exc
    _raw_out = str(out_dir)
    if is_unc_shape(_raw_out) and not unc_probe_allowed(_raw_out):
        raise ExportRefused(
            f"--out {out_dir} is a UNC path outside the trusted roots. Building there would "
            f"reach that host over SMB before this build could check anything about it, and a "
            f"Windows SMB touch carries an NTLM exchange. Point --out at a local directory."
        )


def _refuse_unusable_parent(path: Path, *, what: str) -> None:
    """Refuse before ``mkdir`` when a component of the destination cannot hold a directory.

    ``mkdir(parents=True)`` raises a bare ``NotADirectoryError`` (or ``FileExistsError``)
    when an existing component of the path is a FILE. That escapes as a traceback from a CLI
    whose every other refusal is an ``ExportRefused`` naming the flag at fault, so the
    operator gets a stack trace where they should get "point --out somewhere else".

    ``_is_redirecting_entry`` rather than ``is_dir()``: a junction reports as a directory on
    Windows, and creating directories through one writes wherever it names.
    """
    for ancestor in (path.parent, *path.parent.parents):
        if _is_redirecting_entry(ancestor):
            raise ExportRefused(
                f"cannot write {what}: {ancestor} on the way to {path} is a link or "
                f"junction, and creating directories through it would write outside the "
                f"path you named. Point --out at a plain directory."
            )
        if ancestor.exists():
            if not ancestor.is_dir():
                raise ExportRefused(
                    f"cannot write {what}: {ancestor} exists and is not a directory, so "
                    f"{path} cannot be created under it. Point --out elsewhere."
                )
            return


def _open_dir_nofollow_pinned(dir_path: Path) -> int:
    """Open *dir_path* as a directory fd, pinning EVERY component against a redirect swap.

    ``os.open(str(dir_path), O_RDONLY | O_DIRECTORY)`` opens by re-resolving the whole path
    string, so a symlink at a PARENT or intermediate component is followed -- and a leaf write
    or read taken ``dir_fd``-relative to that descriptor then lands wherever the link named,
    outside ``--out``. The leaf ``O_NOFOLLOW`` guards only the last component; the parent open
    is the hole. This walks the path one component at a time from its anchor, opening each with
    ``O_RDONLY | O_DIRECTORY | O_NOFOLLOW`` relative to the previous descriptor, so a component
    swapped for a link fails its OWN open -- there is no path string re-resolved after a check.
    The caller owns the returned fd and must close it.

    RESOLVED FIRST, deliberately. A per-component ``O_NOFOLLOW`` walk over an UNRESOLVED path
    refuses at the first ordinary symlink -- and a normal home directory is often itself a
    symlink (measured: ``/home/<user>`` resolves elsewhere), so walking an unresolved path
    under ``$HOME`` would refuse every build. ``resolve()`` collapses those legitimate links
    once, up front; walking the resolved components no-follow then makes a refusal mean
    "a component changed AFTER resolution" -- the swap this defends against -- rather than "this
    machine has a normal home". A residual resolve-to-walk window remains (``resolve`` follows
    links at its own call), which is the same narrowing the openat readers accept.

    Falls back to the plain parent open where ``dir_fd`` is unsupported (Windows), the same
    trade the rest of the module makes; the whole builder refuses on that platform up front.
    """
    if not _dir_fd_supported():
        return os.open(str(dir_path), os.O_RDONLY | os.O_DIRECTORY)
    resolved = dir_path.resolve()
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    cur_fd = os.open(resolved.anchor or "/", dir_flags)
    open_dirs = [cur_fd]
    try:
        for part in resolved.relative_to(resolved.anchor).parts:
            cur_fd = os.open(part, dir_flags, dir_fd=open_dirs[-1])
            open_dirs.append(cur_fd)
    except BaseException:
        for d in open_dirs:
            os.close(d)
        raise
    # Close every intermediate but keep the final descriptor for the caller.
    for d in open_dirs[:-1]:
        os.close(d)
    return open_dirs[-1]


def _write_bytes_nofollow(
    path: Path, data: bytes, *, mode: int = 0o600, exclusive: bool = False
) -> None:
    """Write *data* to *path* without following a link that is already there.

    Call sites all write to a path DERIVED from ``--out`` in a directory this build does not
    own -- the staging marker, the machine-readable report, and every staged bundle leaf. A
    plain ``write_bytes``/``write_text`` at any of them follows a link an adversary can
    pre-plant and truncates its target, which is the defect this closes. Writes RAW BYTES so a
    caller carrying a byte-exact signed artifact (the carried plan) gets it verbatim.

    What it does NOT do is decide ownership. The first version unlinked whatever was at the
    path, trading a symlink-follow for deleting an operator's file; the second refused any
    existing path, which broke rebuilding over the same ``--out`` -- the report from our own
    previous run legitimately sits there. Both were wrong in the same way: this function
    cannot tell whose file it is looking at, so it must not act on a guess.

    So the rule is narrow and about SHAPE. ``O_NOFOLLOW`` refuses a symlink, ``EISDIR``
    refuses a directory, and a regular file is truncated in place -- which is what pointing
    ``--out`` at an existing bundle already means. Nothing leaves the directory the operator
    named, which is the property that was actually missing.

    *exclusive* adds ``O_EXCL`` for a caller that has separately established the path should
    not exist yet. The staging marker uses it: a stranger's file there authorises a
    recursive delete, so that path needs more than shape, and its caller checks ownership
    before anything is created.

    Falls back to a plain write where ``dir_fd`` is unsupported, which is Windows.
    """
    if not _dir_fd_supported():
        # The shape refusals still apply here; only the mechanism differs. A directory at
        # this path reports IsADirectoryError on POSIX but PermissionError (EACCES) on
        # Windows, where opening a directory for writing is simply denied, so the shape is
        # judged BEFORE the write rather than translated out of whichever errno the platform
        # chose. Without this the Windows run raised a bare PermissionError and escaped the
        # module's contract to refuse cleanly.
        if _is_redirecting_entry(path):
            raise ExportRefused(
                f"{path} is a symlink. This build writes its own files there and will "
                f"not write through a link to somewhere else. Remove it, or point "
                f"--out elsewhere."
            )
        if path.is_dir():
            raise ExportRefused(
                f"{path} is a directory. This build needs that exact path for a file it "
                f"writes, and it will not delete a directory to get it. The path is "
                f"derived from --out; move it, or point --out elsewhere."
            )
        if exclusive and path.exists():
            raise ExportRefused(
                f"{path} already exists and this build did not write it. The path is "
                f"derived from --out, and building would replace it. Move it, or point "
                f"--out elsewhere."
            )
        # Spelled with an explicit call so this line is not textually identical to any other
        # write in the file. Two identical spellings made a source-substring mutation test land
        # on whichever came first in the file, which was this one -- a branch no POSIX run
        # takes, so the test passed while proving nothing.
        if not path.parent.is_dir():
            # The same refusal the descriptor branch gives, because the guard was added there
            # only and this branch reached the write with an absent parent -- raising a
            # bare FileNotFoundError on the one platform no local test runs. The Windows shard
            # caught it, which is the argument for having that shard.
            raise ExportRefused(
                f"cannot write {path.name}: its directory {path.parent} is not there, or is "
                f"not a directory this build can open. The path is derived from --out, so "
                f"point --out at a directory that exists."
            )
        path.write_bytes(data)
        return
    flags = os.O_WRONLY | os.O_CREAT | _NOFOLLOW_READ_FLAGS
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    try:
        parent_fd = _open_dir_nofollow_pinned(path.parent)
    except OSError as exc:
        # Refused, not raised. The write genuinely cannot proceed without a parent, but this
        # module's contract is to refuse with a message naming what an operator should do --
        # and every path here is derived from --out, so the operator can act on it. A redirect
        # at a parent component also arrives here (its own no-follow open fails), so a swapped
        # parent is refused rather than followed outside --out.
        raise ExportRefused(
            f"cannot write {path.name}: its directory {path.parent} is not there, is not a "
            f"directory this build can open, or a component of it changed to a link ({exc}). "
            f"The path is derived from --out, so point --out at a directory that exists."
        ) from exc
    try:
        try:
            fd = os.open(path.name, flags, mode, dir_fd=parent_fd)
        except IsADirectoryError as exc:
            raise ExportRefused(
                f"{path} is a directory. This build needs that exact path for a file it "
                f"writes, and it will not delete a directory to get it. The path is "
                f"derived from --out; move it, or point --out elsewhere."
            ) from exc
        except FileExistsError as exc:
            raise ExportRefused(
                f"{path} already exists and this build did not write it. The path is "
                f"derived from --out, and building would replace it. Move it, or point "
                f"--out elsewhere."
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ExportRefused(
                    f"{path} is a symlink. This build writes its own files there and will "
                    f"not write through a link to somewhere else. Remove it, or point "
                    f"--out elsewhere."
                ) from exc
            # Any other write failure (ENOSPC, EACCES, EIO) is a genuine failure to WRITE, not
            # an ambiguous "unreadable read to interpret" -- so it is propagated deliberately.
            # Every caller is inside build_bundle's transaction, whose ``except BaseException``
            # rollback removes the staging tree and marker, so a propagated OSError aborts the
            # build cleanly rather than leaking. Converting it to ExportRefused here would only
            # relabel a real I/O failure; the honest report is the OSError.
            raise
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    finally:
        os.close(parent_fd)


def _write_nofollow(path: Path, text: str, *, mode: int = 0o600, exclusive: bool = False) -> None:
    """Write *text* (UTF-8) to *path* without following a link that is already there.

    Thin wrapper over :func:`_write_bytes_nofollow`: the payload is encoded once, with
    ``newline=""`` semantics (no CRLF translation), so the shape refusals, the descriptor-
    relative no-follow open, and the byte-exact write all live in one place. See that function
    for the ownership rule and why the write must not follow a planted link.
    """
    _write_bytes_nofollow(path, text.encode("utf-8"), mode=mode, exclusive=exclusive)


def _write_marker_exclusive(path: Path, *, ours: bool = False) -> None:
    """Create the staging marker at ``<out>.staging.owned``, refusing a planted link.

    The mechanism is in :func:`_write_nofollow`; this names the payload and keeps the call
    site readable. It is a separate function because the marker's BODY is what
    ``_marker_is_ours`` reads back, so the two belong beside each other.

    *ours* is passed through from the caller's own ownership check. On the resume path OUR
    marker legitimately exists and must be replaced; on a fresh build any existing file is
    a stranger's and is refused. The caller is the only place that knows which case it is,
    because it is the one that ran ``_marker_is_ours`` before touching staging.
    """
    _write_nofollow(path, _STAGING_MARKER_BODY, exclusive=not ours)


def _marker_lines_are_this_run(fh: "IO[str]") -> bool:
    """Whether an open marker names this builder AND this run.

    Both lines, because either alone is the wrong question. Without the token any file
    passes; without the run id a CONCURRENT build's marker passes, and the recursive delete
    the marker authorises then removes a staging tree another build is still writing.

    A marker from an earlier run of this same builder is deliberately NOT ours. That is a
    behaviour change: such a marker does NOT authorise the delete, which is how a crashed run's
    residue got cleaned up automatically. It now has to be removed by hand, and the refusal
    says so -- the alternative is being unable to tell a crashed run's leftovers from a live
    run's working directory, and only one of those is safe to delete.
    """
    return fh.readline().strip() == _STAGING_MARKER_TOKEN and fh.readline().strip() == _RUN_ID


def _marker_is_ours(path: Path) -> bool:
    """True only for a marker this builder wrote, read without following a link.

    ``is_file()`` was the whole check and it is true of any plain file, so the ownership
    proof that authorises ``shutil.rmtree`` was satisfied by a file the operator put
    there. The token has to be present, and the read has to refuse a symlink for the same
    reason the write does: a link here would let the answer come from a file outside the
    directory being judged.

    Falls back to a plain read where ``dir_fd`` is unsupported (Windows), matching the
    write. The token check still holds there; what is lost is the anchoring, and losing it
    on the platform whose links behave differently anyway is the same trade the rest of
    this module already makes.
    """
    if not _dir_fd_supported():
        # Judged by ``lstat`` before the open, because this branch has no anchoring to lose
        # the race with: ``path.open`` follows a symlink AND a junction, so a marker path
        # someone planted a redirect over would be read through to its target. The verdict
        # matches the anchored branch below, where ``O_NOFOLLOW`` answers ELOOP and this
        # function returns False -- a redirect at the marker path is not a marker this run
        # wrote, on either platform.
        if _is_redirecting_entry(path):
            return False
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
                return _marker_lines_are_this_run(fh)
        except OSError:
            return False
    try:
        parent_fd = _open_dir_nofollow_pinned(path.parent)
    except OSError:
        # No parent directory, so no marker -- the ordinary first build into a path whose
        # parent does not exist yet. This open sat OUTSIDE the guard below, so
        # `--out new/nested/bundle` raised an unhandled FileNotFoundError out of a function
        # whose entire job is to answer yes or no. A file where the parent should be
        # (NotADirectoryError), a permission failure, and a parent component swapped to a link
        # (the pinning walk fails its own open) all get the same answer for the same reason:
        # none of them is a marker this run wrote.
        return False
    try:
        fd = os.open(path.name, os.O_RDONLY | _NOFOLLOW_READ_FLAGS, dir_fd=parent_fd)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return False
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            return False  # a symlink at the marker path is not our marker
        # Any other open failure (EACCES on a marker that exists, an I/O error) means we
        # CANNOT confirm this marker is one this run wrote. This function's contract is a
        # bool -- "is this our marker?" -- and the safe answer to "cannot tell" is False:
        # a marker we cannot read is treated as not-ours, which makes the caller refuse to
        # reuse the staging tree rather than delete on an unverified marker. Re-raising the
        # raw OSError instead would escape a bool-returning function as a foreign type.
        return False
    finally:
        os.close(parent_fd)
    # The read is inside its own guard because ``os.open(O_RDONLY)`` SUCCEEDS on a
    # directory and it is ``fdopen`` in text mode that fails, with an IsADirectoryError
    # naming a file descriptor. Guarding only the open let that escape as a raw traceback
    # from a question whose answer is simply "no".
    try:
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace", newline="") as fh:
            return _marker_lines_are_this_run(fh)
    except (IsADirectoryError, UnicodeError):
        return False


def skill_candidates(skills_root: Path) -> list[Candidate]:
    """Skill directories (each dir holding a ``SKILL.md``), deny-by-default.

    Skills are global on the owner's machine and many drive ``gh``, an AWS
    profile, Playwright or the loopback gateway -- none of which exist in a
    customer-facing container -- so selection is a deployment judgement and every
    skill starts excluded.
    """
    if _is_redirecting_entry(skills_root):
        # Judged BEFORE ``is_dir()``, which follows the link: a symlinked or
        # junctioned ``skills`` root makes ``rglob("SKILL.md")`` below enumerate a
        # tree OUTSIDE ``--source``, and every match's ``relative_to(skills_root)``
        # still reads in-bounds, so files sourced elsewhere are selectable and ship
        # in the bundle. This is the redirect class the per-entry guard (below) and
        # ``_refuse_redirects_in_chain`` already block at the SKILL.md and the
        # out/staging/previous paths; the root itself was the uncovered variant.
        # Refused, not skipped: a silently empty skills list looks like a deliberate
        # persona-only choice, which is exactly the omission a redirected root hides.
        raise ExportRefused(
            f"the skills root {skills_root} is a link or junction. Enumerating skills "
            f"through it would walk a tree outside --source while every id still reads "
            f"in-bounds, so files sourced elsewhere would ship in the bundle. Refusing "
            f"to traverse a redirected skills root; point --source at a real directory."
        )
    if not skills_root.is_dir():
        # ``not is_dir()`` conflates two cases that must not share an answer, because a
        # directory's SHAPE is author-supplied input (via --source / the crew home) just as
        # much as a spec field is. A genuinely ABSENT root is the ordinary persona-only crew;
        # a root that EXISTS but is not a directory -- a plain file, a FIFO, a device where the
        # ``skills`` directory should be -- is a MALFORMED structure, and shipping an empty
        # bundle for it is the same silent-omission trap as a dropped skill asset: the operator
        # gets a plausible-looking persona-only bundle instead of being told their layout is
        # wrong. So the wrong-TYPE case is REFUSED (the author-supplied-structure rule:
        # absent -> empty, wrong-type -> refuse, unreadable -> fail closed), and only the
        # absent case warns.
        if _is_redirecting_entry(skills_root) or skills_root.exists():
            raise ExportRefused(
                f"the skills root {skills_root} exists but is not a directory. It is derived "
                f"from the crew home (--source / KIROCREW_HOME), and a non-directory there is "
                f"a malformed layout, not an empty skill set; refusing rather than ship a "
                f"bundle that silently omits every skill. Point --source at a real crew home."
            )
        # A missing skills root is the silent-omission trap fix #3 addresses: the
        # curation scans a directory that does not exist, finds nothing, and
        # produces a bundle with no skills that looks like a deliberate choice. A
        # crew with genuinely zero skills is legitimate (many crews ship persona
        # only), so this is a warning, not a refusal -- but it is LOUD, on stderr,
        # naming the path, so an operator who expected skills sees the cause
        # (usually a wrong home or an unset KIROCREW_HOME) rather than a
        # plausible-looking empty bundle.
        print(
            f"WARNING: skills root {skills_root} does not exist; the bundle will "
            f"contain NO skills. If this crew is meant to have skills, check the "
            f"crew home (KIROCREW_HOME / --source). If it is persona-only, ignore "
            f"this.",
            file=sys.stderr,
        )
        return []
    out: list[Candidate] = []
    for skill_md in _walk_no_reparse(skills_root, match="SKILL.md"):
        skill_dir = skill_md.parent
        rel = skill_dir.relative_to(skills_root).as_posix()
        # A component between the skills root and this SKILL.md that redirects (a symlink or a
        # Windows junction) is refused BEFORE ``is_file()``/``_read_text`` below, because those
        # resolve the path and on Windows resolving a junction to a UNC share is an outbound
        # SMB/NTLM probe. The root-junction guard covers only the skills root; a NESTED junction
        # is reached here, so ``_redirect_between`` walks each component and blocks the skill if
        # any redirects. (``rglob`` has already listed the name; this stops the resolving read.)
        crossed = _redirect_between(skills_root, skill_md)
        if crossed is not None:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=(
                        f"reached through a link or junction at "
                        f"{crossed.relative_to(skills_root).as_posix()}; its location is "
                        f"outside the crew source, so it is not shipped"
                    ),
                )
            )
            continue
        # The SKILL.md must be a readable regular file of UTF-8 text, judged HERE, because
        # ``rglob("SKILL.md")`` matches the NAME and everything after it assumed content.
        #
        # A FIFO, a device node, a directory called SKILL.md, or a file that is not UTF-8 all
        # reached this list. The credential scan then skipped them -- ``_read_text`` returns
        # None for content it cannot decode and the loop below does ``continue`` -- so the
        # skill passed unblocked, was selectable, and shipped a bundle whose skill has no
        # usable instructions. Worse for the FIFO: the scan's own read blocks forever on a
        # pipe with no writer, so the build hangs instead of finishing.
        #
        # Blocked rather than dropped, so the notes name it. A skill silently missing from
        # the plan looks like a skill that was never there.
        if _is_redirecting_entry(skill_md) or not skill_md.is_file():
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=(
                        "SKILL.md is not a regular file (it is a link, a directory or a "
                        "special file), so there is nothing to ship for this skill"
                    ),
                )
            )
            continue
        if _read_text_openat(skills_root, skill_md.relative_to(skills_root)) is None:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=(
                        "SKILL.md is not UTF-8 text, so the container could not read it and "
                        "the credential scan could not read it either"
                    ),
                )
            )
            continue
        # Credential store inside the skill => blocked, never includable. Both
        # halves apply, mirroring _copy_skill and _resolve_prompt_path: a file
        # NAMED like a credential (refused_by_name) and a file LOCATED inside a
        # credential directory (refused_by_location, e.g. a nested .aws/config
        # whose basename is innocent). Catching the location half here reports
        # the skill as blocked in the curation plan rather than letting it look
        # selectable and only failing at copy time.
        #
        # A directory junction inside the skill is checked FIRST: ``rglob`` descends into it
        # and the files under it report ``is_symlink()`` False, so both credential scans below
        # would read (or fail to read) the junction target's files as if in-tree. Blocking the
        # skill on any redirecting component keeps content whose true location is outside the
        # source from being scanned-as-clean and later copied.
        redirect = next(
            (p for p in _walk_no_reparse(skill_dir) if _is_redirecting_entry(p)),
            None,
        )
        if redirect is not None:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=f"reaches outside the source through a link or junction: "
                    f"{redirect.relative_to(skill_dir).as_posix()}",
                )
            )
            continue
        cred_file = next(
            (
                p
                for p in _walk_no_reparse(skill_dir)
                if p.is_file()
                and _redirect_between(skill_dir, p) is None
                and (refused_by_name(p) or refused_by_location(p))
            ),
            None,
        )
        if cred_file is not None:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=f"contains a credential store: "
                    f"{cred_file.relative_to(skill_dir).as_posix()}",
                )
            )
            continue
        # A hard credential in any readable file blocks the skill too.
        hard_hit = ""
        for p in _walk_no_reparse(skill_dir):
            if not p.is_file() or p.is_symlink():
                continue
            text = _read_text_openat(skill_dir, p.relative_to(skill_dir))
            if text is None:
                continue
            leaks = scan_text(text, f"skills/{rel}/{p.relative_to(skill_dir).as_posix()}")
            if leaks:
                hard_hit = f"contains a credential -- {leaks[0].render()}"
                break
        if hard_hit:
            out.append(Candidate(kind="skills", id=rel, content_hash="", blocked=hard_hit))
            continue
        out.append(Candidate(kind="skills", id=rel, content_hash=_tree_hash(skill_dir)))
    return out


def _canonical_server(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True, ensure_ascii=False)


def mcp_candidates(agent_spec: dict) -> list[Candidate]:
    """MCP servers declared by the crew's agent spec, deny-by-default.

    Ported from ``crew_export/candidates.py:mcp_candidates``: a server reasonable
    on the owner's laptop may be a customer-reachable side effect in production,
    so tool surface is a deployment decision and an empty ``mcp.json`` is the
    expected outcome, not a degraded one.
    """
    servers = agent_spec.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    out: list[Candidate] = []
    for name, spec in sorted(servers.items()):
        if not isinstance(spec, dict):
            continue
        canonical = _canonical_server(spec)
        if name in _CONTAINER_OWNED_MCP:
            out.append(
                Candidate(
                    kind="mcp",
                    id=name,
                    content_hash=_sha(canonical.encode("utf-8")),
                    blocked="a Kiro Crew-managed server that resolves to an absolute "
                    "path on this machine; the container composes its own",
                )
            )
            continue
        leaks = scan_text(canonical, f"mcp/{name}")
        blocked = f"contains a credential -- {leaks[0].render()}" if leaks else ""
        out.append(
            Candidate(
                kind="mcp",
                id=name,
                content_hash=_sha(canonical.encode("utf-8")),
                blocked=blocked,
            )
        )
    return out


# ===========================================================================
# The crew source.
# ===========================================================================
@dataclass(frozen=True)
class ResolvedCrew:
    name: str
    agent_spec_path: Path
    skills_root: Path


def _default_kiro_home() -> Path:
    override = os.environ.get("KIRO_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kiro"


def _default_config_dir() -> Path:
    override = os.environ.get("KIROCREW_HOME")
    if override:
        return Path(override).expanduser()
    # The repo's real convention is ~/.kiro/crew, NOT ~/.kirocrew. Kiro Crew's
    # config_dir() defaults here (config/paths.py:44 CONFIG_DIR_NAME=".kiro/crew",
    # :93 "default data root: ~/.kiro/crew") and skills live at config_dir()/skills
    # (config/sections.py: "Local ~/.kiro/crew/skills/ takes precedence"). The
    # wrong default (~/.kirocrew) appeared nowhere else in the tree and, with
    # KIROCREW_HOME unset, made curation scan a directory that does not exist,
    # find no skills, and produce a bundle that silently omitted them. Line 369
    # of this file already uses ~/.kiro for the agent home; this now agrees.
    return Path.home() / ".kiro" / "crew"


def _validated_crew_name(name: str) -> str:
    """A crew name is a NAME. Reject anything that can address a path.

    ``agent_spec_path`` was built as ``source / "agents" / f"{name}.json"``, and
    ``Path.__truediv__`` treats an absolute segment as a new root and a ``..`` segment as a
    parent step. So ``--crew ../../secrets`` read a JSON file outside the selected source
    and bundled its contents, and an absolute name discarded the source entirely.

    ``--crew`` is operator-supplied rather than attacker-supplied, so this is hardening
    rather than a breach: the value cannot be set by the untrusted crew content the rest of
    this module defends against. It is still worth refusing, because the operator's typo
    and the operator's paste are the same shape as the attack, and a name that resolves
    outside the source they named is never what they meant.

    Kept deliberately narrow: separators of either platform, parent steps, absolute paths,
    a Windows drive, and the empty name. Everything else a filesystem accepts in a filename
    is still a legal crew name.
    """
    if not name or name in {".", ".."}:
        raise ExportRefused(f"crew name {name!r} is empty or a directory reference.")
    if "/" in name or "\\" in name or "\x00" in name:
        raise ExportRefused(
            f"crew name {name!r} contains a path separator. A crew name addresses one file "
            f"inside the source's agents/ directory, so a name that can leave that "
            f"directory is refused."
        )
    if os.path.isabs(name) or (len(name) > 1 and name[1] == ":"):
        raise ExportRefused(
            f"crew name {name!r} is an absolute path. Joining it would discard the source "
            f"directory entirely, so the spec read would come from somewhere --source never "
            f"named."
        )
    return name


def resolve_crew(name: str, source: Path | None) -> ResolvedCrew:
    """Resolve a crew's agent spec and skills root.

    With ``--source`` (or ``$SMC_CREW_SOURCE``) the root holds ``agents/`` and
    ``skills/`` -- the shape a test fixture provides. Without it, the real
    locations are used: the agent spec under ``$KIRO_HOME``/``~/.kiro/agents``
    and skills under ``$KIROCREW_HOME``. Never a temp dir.
    """
    name = _validated_crew_name(name)
    if source is not None:
        # ONE guard, not two. A containment assertion on the resolved spec path was here as
        # defence in depth, and it is unreachable: with the name check above in place no
        # value gets far enough to land outside ``agents/``, so no test could redden it. A
        # guard no test can fail is a comment claiming a property nobody verifies, so it is
        # gone rather than shipped. If the join ever changes shape, the check to add back is
        # one that can be tested against the new shape.
        return ResolvedCrew(
            name=name,
            agent_spec_path=source / "agents" / f"{name}.json",
            skills_root=source / "skills",
        )
    return ResolvedCrew(
        name=name,
        agent_spec_path=_default_kiro_home() / "agents" / f"{name}.json",
        skills_root=_default_config_dir() / "skills",
    )


def read_agent_spec(crew: ResolvedCrew) -> dict:
    _refuse_without_nofollow_primitive()
    path = crew.agent_spec_path
    # The same fence the prompt reference gets, on the same reasoning: the spec's bytes SHIP,
    # as ``agent.json`` inside the bundle, so this read reaches the customer just as directly
    # as an inlined prompt does. ``--source`` is the operator's flag and the crew name is
    # validated, so the shape ``<source>/agents/<name>.json`` is narrow -- but "narrow" was
    # the argument for the local denylist that three review rounds each holed, so the answer
    # is to ask the shared question rather than to argue about reach.
    #
    # Unlike the prompt path this does NOT refuse outright when the fence is unimportable:
    # reading the agent spec is the tool's whole purpose and there is no inline alternative
    # to fall back to, so refusing would make the module unusable in the standalone mode it
    # documents. It does not SKIP the question either -- that made standalone the one
    # mode where a sensitive --source was read and bundled. The local list below answers a
    # coarser version of it, and runs in ADDITION to the shared validator, never instead.
    # A symlink at the spec IS refused, below, whatever either fence can say.
    # Spelled as a module import rather than ``from ... import is_sensitive_path``, which is
    # the mutation anchor a test uses to simulate the fence being unimportable at the PROMPT
    # site. ``load_build``'s mutation replaces the FIRST match, and this line sits earlier in
    # the file, so sharing that prefix silently retargeted the mutation onto this line and
    # broke the module instead of testing the prompt fallback.
    # The UNC question comes FIRST, before the sensitive-path fence and before any stat.
    # ``hooks.validate_file_path`` states the reason: ``realpath`` on a UNC path IS the
    # outbound SMB probe, and a Windows SMB touch carries an NTLM exchange. A ``--source``
    # or ``--crew`` naming a share therefore leaks a credential exchange to that host
    # before anything about the path has been judged, and the sensitive-path fence below
    # cannot help -- it reads the NAME, and by the time its verdict matters the stat has
    # already gone out.
    #
    # nt-scoped, and fails CLOSED on an unavailable import, matching the prompt site's
    # gate. This is the one question in this function that is not answerable from a local
    # list: whether resolving a path reaches a host is not a property of its spelling.
    if os.name == "nt":
        try:
            from kiro_crew.hooks import is_unc_shape, unc_probe_allowed
        except ImportError as exc:
            raise ExportRefused(
                f"cannot judge whether the agent spec path {path} names a UNC path, "
                f"because kiro_crew.hooks is not importable here ({exc}). Reading it could "
                f"reach a host over SMB before any check runs, so it is refused rather than "
                f"read unchecked. Point --source at a local crew home."
            ) from exc

        _raw_spec = str(path)
        if is_unc_shape(_raw_spec) and not unc_probe_allowed(_raw_spec):
            raise ExportRefused(
                f"the agent spec path {path} is a UNC path outside the trusted roots. "
                f"Reading it would reach that host over SMB before this build could check "
                f"anything about it, and a Windows SMB touch carries an NTLM exchange. "
                f"Point --source at a local crew home."
            )

    # BEFORE the sensitive-path fence below, not after it. That fence RESOLVES: its own
    # contract is the "fully symlink-RESOLVED canonical target (realpath / Path.resolve --
    # follows every symlink in the chain)", and on Windows following a reparse point that
    # names a share IS the outbound SMB probe with its NTLM exchange. A local path leading
    # through a junction to a share therefore leaks during the fence's own resolution, and a
    # refusal computed afterwards arrives after the packet. The walk below judges each
    # component by lstat and follows nothing, so it is safe to run first and it is the only
    # one of the two that can be.
    # The WHOLE chain below the crew root, not just the final component.
    #
    # ``_is_redirecting_entry(path)`` was the check here and it only judges the last name, so a
    # redirect at the PARENT -- ``<source>/agents`` replaced by a junction -- was traversed by
    # the ``is_file()`` below it. That is the same mistake the prompt fence made in its first
    # version, and the same function fixes it: the walk judges each component by ``lstat`` and
    # never follows one, which is what keeps a Windows reparse point naming a share from being
    # probed before anything has been checked.
    #
    # Anchored at the crew root (``<source>`` or the default Kiro home), which is the operator's
    # own flag rather than crew content. Above that is not this build's business; below it is
    # exactly the part that may have arrived with a downloaded crew.
    _refuse_redirects_in_chain(
        path.parent.parent, f"{path.parent.name}/{path.name}", what="agent spec"
    )

    try:
        from kiro_crew import security as _sec

        _spec_fence: Callable[[str], bool] | None = _sec.is_sensitive_path
    except Exception:  # pragma: no cover - exercised by whichever branch the environment allows
        _spec_fence = None
    _posix = path.as_posix()
    if (_spec_fence is not None and _spec_fence(_posix)) or _looks_sensitive_standalone(_posix):
        raise ExportRefused(
            f"the agent spec path {path} is one this repository treats as sensitive. Its "
            f"bytes ship inside the bundle as agent.json, so it is read under the same fence "
            f"a prompt reference gets. Check --crew / --source."
        )
    # No separate ``is_file()`` before the read: that stat opened a check/read window a
    # concurrent writer could win by loop-swapping the spec between the two. ``_read_text_openat``
    # walks ``agents/<name>.json`` from the crew root opening each component with ``O_NOFOLLOW``
    # via ``dir_fd``, so a redirect at ANY component -- including the ``agents/`` parent swapped
    # after the chain check above -- fails its own open with no path re-resolved between check
    # and read. The chain check stays as the readable refusal for a pre-planted redirect; the
    # openat walk is what closes the RACE the chain check cannot. A missing file, a link, a FIFO
    # or a directory all surface as ``None``; the two errors below keep the "nothing to deploy"
    # case distinguishable from an unreadable one via a non-following stat.
    anchor = path.parent.parent
    text = _read_text_openat(anchor, path.relative_to(anchor))
    if text is None:
        try:
            present = os.lstat(path)
        except OSError:
            present = None
        if present is None:
            raise ExportRefused(
                f"no agent spec for crew {crew.name!r} at {path}. There is nothing to "
                f"deploy; check --crew / --source."
            )
        raise ExportRefused(
            f"agent spec {path} could not be read as UTF-8 (it may be a link, a special "
            f"file, or reached through a redirected parent); refusing rather than following it."
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExportRefused(f"agent spec {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ExportRefused(f"agent spec {path} must be a JSON object")
    return parsed


def enumerate_all(crew: ResolvedCrew, agent_spec: dict) -> dict[str, list[Candidate]]:
    return {
        "skills": skill_candidates(crew.skills_root),
        "mcp": mcp_candidates(agent_spec),
    }


# ===========================================================================
# The curation plan (review file): deny-by-default, signature, content pin.
# Ported from ``crew_export/plan.py`` -- JSON instead of YAML (no PyYAML here).
# ===========================================================================
_KINDS = ("skills", "mcp")

_PLAN_INSTRUCTIONS = (
    "Everything below starts include:false. Flip include:true on the skills and "
    "MCP servers a customer may reach, fill in reviewed_by and reviewed_at, then "
    "pass this file to the build with --allow. Leaving it untouched is valid: you "
    "get a working crew with its persona and no private content. Do not hand-edit "
    "sha256 -- it pins each entry to the content you reviewed; if a SELECTED entry "
    "changes afterwards the build refuses and names it. A 'blocked' entry cannot "
    "be included at all."
)


@dataclass
class Plan:
    crew: str
    reviewed_by: str
    reviewed_at: str
    selections: dict[str, dict[str, bool]]
    pins: dict[str, dict[str, str]]

    def included(self, kind: str) -> set[str]:
        return {cid for cid, on in self.selections.get(kind, {}).items() if on}

    def is_signed(self) -> bool:
        return bool(self.reviewed_by.strip()) and bool(self.reviewed_at.strip())

    def selects_anything(self) -> bool:
        return any(self.included(kind) for kind in _KINDS)


@dataclass
class Drift:
    appeared: int = 0
    vanished: int = 0

    def describe(self) -> str:
        parts = []
        if self.appeared:
            parts.append(f"{self.appeared} new candidate(s) appeared (all excluded)")
        if self.vanished:
            parts.append(f"{self.vanished} candidate(s) no longer exist")
        return "; ".join(parts)


def write_plan(path: Path, crew: str, candidates: dict[str, list[Candidate]]) -> None:
    """Write a fresh deny-by-default review template."""
    body: dict[str, object] = {
        "plan_version": PLAN_VERSION,
        "crew": crew,
        "instructions": _PLAN_INSTRUCTIONS,
        "reviewed_by": "",
        "reviewed_at": "",
    }
    for kind in _KINDS:
        entries = []
        for c in candidates.get(kind, []):
            entry: dict[str, object] = {"id": c.id, "include": False, "sha256": c.content_hash}
            if c.note:
                entry["note"] = c.note
            if c.blocked:
                entry["blocked"] = c.blocked
            entries.append(entry)
        body[kind] = entries
    _refuse_unusable_parent(path, what="the plan")
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" here is uniformity, not correctness: the plan is written before the
    # digest is taken and is carried into the bundle afterwards, so bundle_digest never
    # covers it, and read_plan goes through json.loads, which does not care. It is pinned
    # anyway so that "every write_text in this module pins newline" is a rule with no
    # exceptions -- one a reader can apply from the call site without first working out
    # whether these particular bytes end up hashed. The call that DOES depend on it is
    # _write_guarded; see the note there.
    # Written through ``_write_nofollow`` rather than ``write_text``, which follows a link at
    # the destination. A dangling symlink at the plan path is the worst case: ``write_text``
    # CREATES the link's target, so a plan written to a path an earlier run left linked
    # elsewhere lands wherever it points, with this build's own file mode.
    #
    # ``newline=""`` comes with that writer, and the rule it belongs to is unchanged: every
    # text write in this module pins newline, so a reader can apply it from the call site
    # without first working out whether these particular bytes end up hashed. They do not --
    # the digest is taken before the carried plan is written in -- and the call that DOES
    # depend on it is _write_guarded; see the note there.
    _write_nofollow(path, json.dumps(body, indent=2, ensure_ascii=False) + "\n")


def _require_plan_include(kind: str, cid: str, raw: object) -> bool:
    """A plan entry's ``include`` must be a real JSON boolean.

    ``bool("false")`` is ``True``, so a plan that says ``"include": "false"`` --
    a string, the shape a hand-edited or template-rendered plan easily produces --
    would SELECT the item and ship it in a published bundle, defeating the
    deny-by-default seam this producer exists to enforce. Coercing silently is the
    wrong direction here twice over: it is the OVER-sharing direction the module
    warns against, and it hides that the reviewer's plan does not say what they
    meant. So require a genuine boolean and refuse anything else, in the voice of
    the other ``ExportRefused`` guards. Absent defaults to ``False`` (excluded),
    which is the deny-by-default posture.
    """
    if isinstance(raw, bool):
        return raw
    raise ExportRefused(
        f"curation plan entry {cid!r} in section {kind!r} has a non-boolean "
        f"'include': {raw!r}. It is not coerced because the string \"false\" is "
        f"truthy, so a coercion would SELECT an item the reviewer meant to "
        f"exclude and ship it in the bundle. Write true or false, not a string."
    )


def read_plan(path: Path) -> Plan:
    _refuse_without_nofollow_primitive()
    # The ``--allow`` path is an operator-typed CLI argument, so it can name a UNC share, a
    # sensitive location, or a redirect just like ``--source`` can. It goes through the same
    # three gates the agent-spec read uses, in the same order, so a check-then-read window and
    # an unfenced read cannot let a redirected or sensitive plan path through. UNC first
    # on Windows, before any stat: resolving a UNC path IS the outbound SMB probe and a Windows
    # SMB touch carries an NTLM exchange, so a name fence cannot help once the stat has gone out.
    if os.name == "nt":
        try:
            from kiro_crew.hooks import is_unc_shape, unc_probe_allowed
        except ImportError as exc:
            raise ExportRefused(
                f"cannot judge whether the curation plan path {path} names a UNC path, "
                f"because kiro_crew.hooks is not importable here ({exc}). Reading it could "
                f"reach a host over SMB before any check runs, so it is refused rather than "
                f"read unchecked. Pass --allow a local path."
            ) from exc
        _raw = str(path)
        if is_unc_shape(_raw) and not unc_probe_allowed(_raw):
            raise ExportRefused(
                f"the curation plan path {path} is a UNC path outside the trusted roots. "
                f"Reading it would reach that host over SMB before this build could check "
                f"anything about it. Pass --allow a local path."
            )
    try:
        from kiro_crew import security as _sec

        _fence: Callable[[str], bool] | None = _sec.is_sensitive_path
    except Exception:  # pragma: no cover - exercised by whichever branch the environment allows
        _fence = None
    _posix = path.as_posix()
    if (_fence is not None and _fence(_posix)) or _looks_sensitive_standalone(_posix):
        raise ExportRefused(
            f"the curation plan path {path} is inside a credential/sensitive location. "
            f"Refusing to read it. Pass --allow a plan written by the plan command."
        )
    # ``_read_text_openat`` walks the path one component at a time from the filesystem root,
    # opening each with ``O_NOFOLLOW | O_DIRECTORY`` via ``dir_fd``, so a redirect at ANY
    # component fails its own open -- not only the final one. ``_read_text_nofollow`` guards
    # ONLY the last component, so an intermediate directory swapped for a symlink (``--allow
    # /tmp/alias/auth.json`` with ``alias -> ~/.codex``) is followed into a credential file
    # before the leaf open runs, and the literal-component standalone fence above cannot catch
    # it because the resolved location is not spelled in the path. The spec read anchors every
    # component this same way; the plan read must match it. ``path.absolute()`` makes a relative
    # ``--allow`` absolute WITHOUT resolving links (unlike ``resolve()``), so the walk starts at
    # the real root and every component -- including the redirecting one -- is opened no-follow.
    # ``None`` covers a missing file, a link at any component, a special file, or a non-UTF-8
    # body; the two branches keep "no plan" distinct from "unreadable".
    abs_path = path if path.is_absolute() else path.absolute()
    text = _read_text_openat(Path(abs_path.anchor), abs_path.relative_to(abs_path.anchor))
    if text is None:
        try:
            present = os.lstat(path)
        except OSError:
            present = None
        if present is None:
            raise ExportRefused(f"no curation plan at {path}. Run the plan command first.")
        raise ExportRefused(
            f"the curation plan at {path} could not be read as UTF-8 (it may be a link, a "
            f"special file, or not decodable); refusing rather than following it."
        )
    try:
        raw = json.loads(text)
    except (ValueError, OSError) as exc:
        # ``ValueError`` rather than ``json.JSONDecodeError``, because the read happens
        # before the parse and can fail on its own terms: a plan file that is not valid
        # UTF-8 raises ``UnicodeDecodeError``, which is a ``ValueError`` and neither a
        # ``JSONDecodeError`` nor an ``OSError``. It therefore escaped this handler and left
        # ``main`` printing a traceback where this module's contract is to refuse cleanly.
        # ``JSONDecodeError`` is itself a ``ValueError``, so the wider tuple still covers
        # what the narrower one did.
        raise ExportRefused(f"curation plan {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExportRefused(f"curation plan {path} is not an object")
    if raw.get("plan_version") != PLAN_VERSION:
        raise ExportRefused(
            f"curation plan version {raw.get('plan_version')!r} is not {PLAN_VERSION}; "
            f"regenerate it"
        )
    selections: dict[str, dict[str, bool]] = {}
    pins: dict[str, dict[str, str]] = {}
    for kind in _KINDS:
        entries = raw.get(kind) or []
        if not isinstance(entries, list):
            raise ExportRefused(f"curation plan section {kind!r} is not a list")
        sel: dict[str, bool] = {}
        pin: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or "id" not in entry:
                raise ExportRefused(f"malformed entry in {kind!r}: {entry!r}")
            raw_id = entry["id"]
            if not isinstance(raw_id, str):
                raise ExportRefused(
                    f"entry id in {kind!r} is {type(raw_id).__name__} ({raw_id!r}), not a "
                    f"string; it names what the plan selects and cannot be coerced. Fix the "
                    f"plan."
                )
            cid = raw_id
            sel[cid] = _require_plan_include(kind, cid, entry.get("include", False))
            # A non-string ``sha256`` is REFUSED, not ``str()``-coerced: the pin decides whether
            # a skill's bytes match what was reviewed, so a fabricated pin is a fabricated
            # integrity claim in a signed plan. Absent (None/missing) is legitimate -- it means
            # no pin -- and stays the empty string.
            raw_sha = entry.get("sha256")
            if raw_sha is not None and not isinstance(raw_sha, str):
                raise ExportRefused(
                    f"'sha256' for {cid!r} in {kind!r} is {type(raw_sha).__name__} "
                    f"({raw_sha!r}), not a string; a content pin cannot be coerced. Fix the plan."
                )
            pin[cid] = raw_sha or ""
        selections[kind] = sel
        pins[kind] = pin
    # The plan's identity/provenance fields feed the signed-plan guard, so a non-string is
    # REFUSED rather than ``str()``-coerced, the same rule the ``sha256`` pin above states: a
    # coerced ``reviewed_by`` or ``reviewed_at`` fabricates provenance the signature is taken
    # over, and a coerced ``crew`` fabricates which crew the plan claims to be for. Absent
    # (None/missing) stays the empty string, which is a legitimate "unsigned/unstated" plan.
    for _field in ("crew", "reviewed_by", "reviewed_at"):
        _val = raw.get(_field)
        if _val is not None and not isinstance(_val, str):
            raise ExportRefused(
                f"plan field {_field!r} is {type(_val).__name__} ({_val!r}), not a string; "
                f"it is provenance the signed-plan guard reads and cannot be coerced. Fix "
                f"the plan."
            )
    return Plan(
        crew=str(raw.get("crew") or ""),
        reviewed_by=str(raw.get("reviewed_by") or ""),
        reviewed_at=str(raw.get("reviewed_at") or ""),
        selections=selections,
        pins=pins,
    )


def verify(plan: Plan, crew: str, candidates: dict[str, list[Candidate]]) -> Drift:
    """Refuse unless signed and every selected item is byte-for-byte as reviewed.

    Ported from ``crew_export/plan.py:verify``. Drift outside the selection is
    reported, never refused on: a file the operator did not choose cannot reach
    the bundle, so blocking on it is a false alarm.
    """
    if plan.crew != crew:
        raise ExportRefused(f"plan was written for crew {plan.crew!r}, not {crew!r}")
    if not plan.is_signed():
        raise ExportRefused(
            "curation plan is unreviewed: reviewed_by and reviewed_at are blank. "
            "Read the plan, choose what customers may reach, sign it, then build. "
            "There is deliberately no flag to skip this."
        )
    by_kind = {kind: {c.id: c for c in candidates.get(kind, [])} for kind in _KINDS}
    drift = Drift()
    for kind in _KINDS:
        live = set(by_kind[kind])
        planned = set(plan.selections.get(kind, {}))
        drift.appeared += len(live - planned)
        drift.vanished += len(planned - live)
        for cid in plan.included(kind):
            candidate = by_kind[kind].get(cid)
            if candidate is None:
                raise ExportRefused(f"plan selects {kind}/{cid!r}, which no longer exists")
            if candidate.blocked:
                raise ExportRefused(
                    f"plan selects {kind}/{cid!r}, which cannot be included: {candidate.blocked}"
                )
            pinned = plan.pins.get(kind, {}).get(cid, "")
            if not pinned:
                raise ExportRefused(
                    f"plan selects {kind}/{cid!r} with no recorded content hash, so "
                    f"what was approved cannot be established. Re-run the plan."
                )
            if pinned != candidate.content_hash:
                raise ExportRefused(
                    f"{kind}/{cid} changed after it was approved, so the approval no "
                    f"longer covers it.\n  reviewed: {pinned}\n  current:  "
                    f"{candidate.content_hash}\nRe-run the plan command and look again."
                )
    return drift


def merge_plans(paths: list[Path], crew: str) -> Plan | None:
    """Union the selections of one or more signed review files.

    Each file must match the crew and, if it selects anything, be signed;
    otherwise its selections are refused rather than silently ignored. Returns
    ``None`` when no ``--allow`` was given (pure deny-by-default: an empty
    bundle).
    """
    if not paths:
        return None
    merged_sel: dict[str, dict[str, bool]] = {k: {} for k in _KINDS}
    merged_pins: dict[str, dict[str, str]] = {k: {} for k in _KINDS}
    reviewers: list[str] = []
    reviewed_ats: list[str] = []
    for p in paths:
        plan = read_plan(p)
        if plan.crew != crew:
            raise ExportRefused(f"--allow {p} was written for crew {plan.crew!r}, not {crew!r}")
        if plan.selects_anything() and not plan.is_signed():
            raise ExportRefused(
                f"--allow {p} selects items but is unreviewed (reviewed_by / "
                f"reviewed_at are blank). Sign it or its selections are refused."
            )
        if plan.is_signed():
            reviewers.append(plan.reviewed_by)
            reviewed_ats.append(plan.reviewed_at)
        for kind in _KINDS:
            for cid, on in plan.selections.get(kind, {}).items():
                merged_sel[kind][cid] = merged_sel[kind].get(cid, False) or on
                pin = plan.pins.get(kind, {}).get(cid, "")
                if not pin:
                    continue
                # A pin is only meaningful from a plan that SELECTS the item. The
                # signature check above lets a plan selecting nothing through
                # unsigned, which is correct on its own terms, but the old merge
                # took that plan's pins anyway and the last writer won. So an
                # unsigned plan that selected nothing could replace the content
                # hash a SIGNED plan was reviewed against, and verification would
                # then accept content no reviewer ever saw. Selection is what an
                # approval is about, so it is also what licenses a pin.
                if not on:
                    continue
                prev = merged_pins[kind].get(cid)
                if prev is not None and prev != pin:
                    # Two selecting plans disagreeing about the content is not
                    # something to resolve by ordering. Whichever we picked, one
                    # reviewer approved something else.
                    raise ExportRefused(
                        f"two --allow plans select {kind} {cid!r} but pin different "
                        f"content ({prev} and {pin}). One of the two reviewers "
                        f"approved content this build would not ship, so neither "
                        f"pin is used. Re-review against a single revision."
                    )
                merged_pins[kind][cid] = pin
    return Plan(
        crew=crew,
        reviewed_by="; ".join(sorted(set(r for r in reviewers if r))),
        reviewed_at="; ".join(sorted(set(a for a in reviewed_ats if a))),
        selections=merged_sel,
        pins=merged_pins,
    )


# ===========================================================================
# Spec build -- inline the prompt, normalise tools/MCP.
# Ported from ``crew_export/spec.py`` and the reader guards in
# ``serving/smc/bundle.py`` (validate_prompt, validate_tool_refs).
# ===========================================================================


def _inline_prompt(spec: dict, crew_name: str, agents_dir: Path, notes: list[str]) -> None:
    """Require the prompt to be literal text; refuse a missing one or a file reference.

    Kiro Crew writes an installed agent's prompt as ``file://<absolute host path>``
    (``kiro_crew/agent.py:2166``). That path does not exist in the container, so a naively
    copied spec produces a crew that answers as nobody -- and kiro-cli tolerates an empty
    prompt, so the failure is silent. Refused here
    (``serving/smc/bundle.py:validate_prompt`` refuses it at startup too).

    READING the referenced file is deliberately NOT part of this change. Doing it safely means
    resolving an operator-supplied path without following a redirect, on two platforms with
    different link semantics, before any resolution can reach the network -- roughly 350 lines
    whose review found 20+ separate defects across seven rounds while the rest of this module
    was settled. It ships as its own change, where a reviewer can hold all of it at once.

    So a ``file://`` prompt is refused with an instruction the operator can act on today:
    inline the persona. That is a real limitation and it is stated rather than worked around --
    some shipped agents (``apps/builtins/pptx_maker/agents/*.json``) use the file form, and
    those crews cannot be bundled until the follow-up lands.
    """
    raw = spec.get("prompt")
    if raw is None or not isinstance(raw, str) or not raw.strip():
        raise ExportRefused(
            f"agent.json for {crew_name!r} has no prompt. The prompt is the crew's "
            f"persona and kiro-cli tolerates an empty one, so a crew shipped this way "
            f"answers as nobody. Inline the persona as literal text."
        )
    if raw.strip().lower().startswith("file://"):
        raise ExportRefused(
            f"agent.json for {crew_name!r} references its prompt as a file "
            f"({raw.strip()[:80]!r}). Reading it safely needs the path fences that are "
            f"landing separately, so this build does not follow the reference. Copy the "
            f'persona into the spec\'s "prompt" field as literal text.'
        )
    leaks = scan_text(raw, "prompt")
    if leaks:
        raise ExportRefused("the crew's prompt contains a credential: " + leaks[0].render())


def _clean_mcp_server(name: str, server: dict, notes: list[str]) -> dict:
    """Strip secret-bearing material from one server before it ships.

    ``env`` and ``headers`` are SUPPLEMENTARY and are dropped WHOLESALE, not
    scanned-and-kept. Two reasons this is stricter than
    ``crew_export/spec.py:_clean_mcp_server`` (which keeps benign env): the plan's
    own operator-facing note says "env, headers stripped on export", so keeping
    them contradicts what the owner was told; and a bespoke token format the
    scanner does not recognise would otherwise ship. Dropping them leaves a server
    that fails loudly at connect time -- the safe direction -- and the deployment
    re-supplies whatever the container genuinely needs. This tightening is called
    out in the track report.

    ``args`` and ``url`` are LOAD-BEARING: a credential there refuses the export
    rather than being edited out, because a server minus one arg connects and
    misbehaves. (Ported unchanged from spec.py.)
    """
    out = dict(server)
    for field_name in ("env", "headers"):
        # PRESENT, not "present and a non-empty dict". The type test was there to avoid a
        # note about a field that carried nothing, and it decided the strip as well: a
        # server with ``"env": "TOKEN=sk-live-..."`` or a list of pairs kept the field and
        # shipped it. A malformed value is exactly the one a scanner has no shape for, so
        # the case the type test skipped is the case that most needed dropping.
        if field_name not in out:
            continue
        block = out.pop(field_name)
        if not block:
            continue  # nothing to report, but it is still gone
        # ``len`` only for the shapes that have one. The whole point of this change is that
        # the value may be any type, so the note must not be the thing that raises.
        try:
            count = f"{len(block)} entr(y/ies)"
        except TypeError:
            count = f"a {type(block).__name__} value"
        notes.append(
            f"mcp/{name}: dropped {field_name} ({count}; supplementary and can bear a "
            f"credential, so re-supply via the deployment if needed)"
        )
    for field_name in ("args", "url"):
        value = out.get(field_name)
        if not value:
            continue
        if scan_text(json.dumps(value, ensure_ascii=False), f"mcp/{name}/{field_name}"):
            raise ExportRefused(
                f"MCP server {name!r} carries a credential in {field_name!r}. That "
                f"field cannot be stripped without breaking the server, so the export "
                f"refuses. Move the value into an env var or a vault reference and re-plan."
            )
    return out


@dataclass
class SpecResult:
    spec: dict
    mcp: dict
    notes: list[str] = field(default_factory=list)


def build_spec(
    crew: ResolvedCrew, agent_spec: dict, selected_mcp: set[str], agents_dir: Path
) -> SpecResult:
    """Produce the bundle's ``agent.json`` and ``mcp.json`` from a source spec."""
    notes: list[str] = []
    spec = json.loads(json.dumps(agent_spec))  # detach from the source mapping

    if spec.get("name") != crew.name:
        notes.append(f"renamed spec {spec.get('name')!r} -> {crew.name!r}")
    spec["name"] = crew.name

    _inline_prompt(spec, crew.name, agents_dir, notes)

    for key in _DROPPED_SPEC_KEYS:
        if key in spec:
            spec.pop(key)
            notes.append(f"dropped {key!r}: it is a deployment decision, not the owner's")

    # MCP: keep only what curation approved, cleaned of secret material.
    raw_servers = agent_spec.get("mcpServers")
    source_servers: dict = raw_servers if isinstance(raw_servers, dict) else {}
    mcp: dict[str, dict] = {}
    for name in sorted(selected_mcp):
        server = source_servers.get(name)
        if not isinstance(server, dict):
            raise ExportRefused(
                f"plan selects MCP server {name!r}, which the spec no longer declares"
            )
        mcp[name] = _clean_mcp_server(name, server, notes)
    dropped = sorted(set(source_servers) - set(mcp))
    if dropped:
        notes.append(f"MCP servers not selected: {', '.join(dropped)}")

    # Both files are emitted from this one dict so they cannot drift within a build
    # (crew_export/spec.py records the bug where they did). agent.json stays
    # installable as-is.
    if mcp:
        spec["mcpServers"] = mcp
    else:
        spec.pop("mcpServers", None)

    # tools: a `@server` reference to a server curation removed leaves the crew
    # holding a tool that points at nothing (kiro-cli drops it silently at mount
    # time). `@builtin` is kiro-cli's native group and is NOT an orphan.
    removed_servers = set(source_servers) - set(mcp)

    def _is_orphan(entry: str) -> bool:
        if not entry.startswith("@"):
            return False
        server = entry[1:].split("/", 1)[0]
        return server not in _BUILTIN_TOOL_GROUPS and server in removed_servers

    tools = spec.get("tools")
    # Shape first, and REFUSE rather than ignore. The isinstance(list) branch below quietly
    # skipped a non-list, and then `set(spec.get("tools") or [])` a few lines down hit it
    # anyway: a truthy non-iterable such as `"tools": 3` raised an uncaught TypeError. That
    # crash is loud and happens before anything is written, so nothing was corrupted -- but
    # a traceback tells the operator nothing about which field of which file is wrong, and
    # silently ignoring the value would ship a spec whose tool list is not the one they
    # wrote. allowedTools is checked with it because it feeds the same expression.
    for field_name in ("tools", "allowedTools"):
        value = spec.get(field_name)
        if value is not None and not isinstance(value, list):
            raise ExportRefused(
                f"{field_name!r} in the agent spec is {type(value).__name__}, not a list. "
                f"The bundle's tool grants are computed from it, so a value of another "
                f"shape cannot be narrowed safely. Fix the spec."
            )
    if isinstance(tools, list):
        # REFUSE a non-string element, never ``str()`` it. Coercing a dict/int/list into a
        # tool id fabricates a capability grant nothing in the spec authorized, and the bundle
        # is then SIGNED with it -- worse than a missing tool, which fails visibly at use, an
        # invented one may succeed. Element type is invalid input, so it is named and refused.
        for e in tools:
            if not isinstance(e, str):
                raise ExportRefused(
                    f"'tools' contains a {type(e).__name__} entry ({e!r}), not a string. A "
                    f"tool grant is computed from it and would be fabricated by coercion; the "
                    f"bundle is signed, so an invented capability cannot be allowed. Fix the "
                    f"spec."
                )
        kept = [e for e in tools if not _is_orphan(e)]
        orphans = [e for e in tools if _is_orphan(e)]
        spec["tools"] = kept
        if orphans:
            notes.append("removed tool references with no surviving server: " + ", ".join(orphans))

    # allowedTools cannot inflate past the surviving tools: a grant for a tool the
    # bundle does not carry is dropped.
    final_tools = set(spec.get("tools") or [])
    # REFUSE a non-string allowedTools element rather than silently dropping it (the same
    # invented-vs-omitted concern as tools above: a dropped grant is a silent capability
    # change in a signed bundle). Shape of the list itself is checked at the top of this
    # function; here the elements are.
    raw_allowed = spec.get("allowedTools") or []
    for t in raw_allowed:
        if not isinstance(t, str):
            raise ExportRefused(
                f"'allowedTools' contains a {type(t).__name__} entry ({t!r}), not a string. "
                f"It grants a capability in a signed bundle and cannot be coerced or dropped "
                f"silently. Fix the spec."
            )
    granted = sorted(t for t in raw_allowed if t in final_tools)
    if sorted(raw_allowed) != granted:
        notes.append(f"allowedTools narrowed to surviving tools ({len(granted)} kept)")
    spec["allowedTools"] = granted

    rendered = json.dumps(spec, indent=2, ensure_ascii=False)
    if scan_text(rendered, "agent.json"):
        raise ExportRefused("the agent spec contains a credential after cleaning")

    return SpecResult(spec=spec, mcp=mcp, notes=notes)


# ===========================================================================
# Bundle writer + digest. Ported from ``crew_export/bundle.py``.
# ===========================================================================


def bundle_digest(root: Path, also_skip: frozenset[str] = frozenset()) -> str:
    """sha256 over every bundle file except the manifest, path-and-content, sorted.

    Byte-for-byte the algorithm of ``crew_export/bundle.py:_bundle_digest`` -- the
    "computed the same way bundle.py already does it" the contract points at. The
    manifest is excluded because it carries the digest; the ``sha256:`` prefix and
    the compact JSON row encoding are preserved so the value is reproducible.

    ``also_skip`` holds extra root-relative posix paths to leave out. It defaults to
    nothing, so the contract value is unchanged; the replacement check uses it to
    re-derive a prior bundle's digest while ignoring a plan file that was added
    after that bundle was built.
    """
    rows: list[list[str]] = []
    for path in _walk_no_reparse(root):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "manifest.json" or rel in also_skip:
            continue
        rows.append([rel, hashlib.sha256(path.read_bytes()).hexdigest()])
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_guarded(path: Path, text: str, origin: str) -> None:
    """Last-chance scan before bytes land in the artifact. Refuse on a finding."""
    if scan_text(text, origin):
        raise ExportRefused(f"refusing to write {origin}: it contains a credential")
    _refuse_unusable_parent(path, what=f"{origin}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write through the no-follow primitive, not a plain ``write_text``. The staging tree lives
    # beside ``--out`` in a directory this build does not own, so a leaf path is exactly the
    # mkdir->write window an adversary can plant a symlink into; a following write would then
    # truncate whatever the link named. ``_write_nofollow`` opens the leaf descriptor-relative
    # with ``O_NOFOLLOW`` and refuses a link (the same defence the marker and report already
    # use), and it writes with ``newline=""`` + strict UTF-8 -- the CRLF-translation and
    # encoding contract this site needs so the source pin and the digest stay platform-stable.
    _write_nofollow(path, text)


def _copy_skill(
    skill_dir: Path, rel: str, dest_root: Path, selected: set[str] | None = None
) -> "set[str]":
    """Copy one selected skill, stopping at any nested skill the plan did not select.

    Returns the set of skill-relative posix paths it WROTE, so the staged-tree hash can tell a
    file that was written and then vanished (tampering -> refuse) from one that was never
    staged because it belongs to an unselected nested skill (legitimate -> hashed from source).

    Skills nest: an id is ``relative_to(skills_root).as_posix()``, so ``aws`` and
    ``aws/ec2`` can both be skills and both carry a ``SKILL.md``. A plain ``rglob`` from the
    parent then shipped the child's files too, which defeats deny-by-default -- the plan
    said only ``aws`` and the bundle carried ``aws/ec2`` as well, with no note saying so.

    A descendant is recognised the way the enumerator recognises a skill in the first
    place: it holds a ``SKILL.md``. Its subtree is skipped unless its own id is in
    *selected*, in which case its own ``_copy_skill`` call ships it and this one must not,
    or the same files would be walked twice.

    *selected* defaults to the empty set, which is the SAFE direction: a caller that names
    no selection ships no nested skill. Defaulting to "everything selected" would make the
    old behaviour the fallback, and the old behaviour is the defect.
    """
    selected = selected or set()
    dest = dest_root / rel
    written: set[str] = set()
    excluded_roots = [
        p
        for p in _walk_no_reparse(skill_dir, match="SKILL.md")
        if p.parent != skill_dir
        and f"{rel}/{p.parent.relative_to(skill_dir).as_posix()}" not in selected
    ]
    for p in _walk_no_reparse(skill_dir):
        if not p.is_file() or p.is_symlink():
            continue
        # ``is_symlink()`` does not see a junction, and ``rglob`` descends into one, so a file
        # under a junction would copy into the bundle with its bytes sourced OUTSIDE the crew
        # -- the nested-reparse-point escape the per-SKILL.md check never covered. Refuse it:
        # the copy is where the escape would ship, so a silent skip is not enough.
        redirect = _redirect_between(skill_dir, p)
        if redirect is not None:
            raise ExportRefused(
                f"skill {rel} reaches {p.relative_to(skill_dir).as_posix()} through a link or "
                f"junction at {redirect.relative_to(skill_dir).as_posix()}; its bytes live "
                f"outside the crew source. Refusing to copy content through a redirect."
            )
        if any(root.parent in p.parents or root.parent == p.parent for root in excluded_roots):
            continue
        if refused_by_name(p):
            raise ExportRefused(
                f"skill {rel} contains a credential store: {p.relative_to(skill_dir).as_posix()}"
            )
        if refused_by_location(p):
            # The location half, mirroring _resolve_prompt_path. refused_by_name
            # only fires on a FILE named like a credential, so a nested
            # credential DIRECTORY sails through it: a skill carrying .aws/config
            # or .ssh/known_hosts has innocent basenames (config, known_hosts)
            # and would be copied into a bundle handed to an untrusted agent. A
            # kubeconfig's certificate is base64 and may match no _HARD_PATTERNS
            # entry, so the _write_guarded scan below cannot be relied on to
            # catch it either -- judge the location before the read.
            raise ExportRefused(
                f"skill {rel} contains a file inside a credential directory: "
                f"{p.relative_to(skill_dir).as_posix()}. Files under .ssh, .aws, "
                f".gnupg, .kube or .docker are refused before any read (their "
                f"contents cannot be trusted to be scannable) rather than copied "
                f"into a bundle handed to an untrusted agent."
            )
        text = _read_text_openat(skill_dir, p.relative_to(skill_dir))
        if text is None:
            # Explicit inclusion policy: a file SELECTED for a bundle that cannot be read as
            # scannable UTF-8 is not silently skipped. Silently dropping it shipped the skill
            # incomplete with no notice, and it made "unreadable" read as "not selected" -- the
            # same absent/unreadable/unscannable == not-selected substitution that has surfaced
            # across this file. The safe direction, matching the module's deny-by-default
            # posture, is to REFUSE: an unscannable payload cannot be certified clean, so it
            # must not ship, and the build says which file and why rather than quietly omitting
            # it. An operator who wants a binary asset in a bundle removes it from the skill or
            # ships it another way; the packager does not hand unscanned bytes to an untrusted
            # agent, nor a skill missing files it was told to carry.
            raise ExportRefused(
                f"skill {rel} contains a file that is not scannable UTF-8 text: "
                f"{p.relative_to(skill_dir).as_posix()}. A selected skill's files must be "
                f"readable so the credential scan can clear them; a binary or non-UTF-8 asset "
                f"can be neither scanned nor safely shipped, and is refused rather than "
                f"silently omitted. Remove it from the skill, or ship it outside the bundle."
            )
        _write_guarded(dest / p.relative_to(skill_dir).as_posix(), text, f"skills/{rel}/{p.name}")
        written.add(p.relative_to(skill_dir).as_posix())
    return written


@dataclass
class BuildReport:
    bundle_dir: Path
    digest: str
    skill_count: int
    mcp_servers: list[str]
    denied: list[dict]
    notes: list[str]


def _denied_list(candidates: dict[str, list[Candidate]], plan: Plan | None) -> list[dict]:
    """What did not ship and why, so the owner can see it (SMC_BUNDLE_JSON.denied)."""
    out: list[dict] = []
    for kind in _KINDS:
        included = plan.included(kind) if plan else set()
        for c in candidates.get(kind, []):
            if c.id in included:
                continue
            if c.blocked:
                reason = c.blocked
            elif plan is None:
                reason = "no curation plan supplied (deny-by-default)"
            else:
                reason = "not marked reviewed in the plan (deny-by-default)"
            out.append({"kind": kind, "id": c.id, "reason": reason})
    return out


def _refuse_unless_this_build_wrote_it(d: Path, flag: str) -> None:
    """Refuse ``d`` unless every rule says this build produced it. Raises ``ExportRefused``.

    Three rules, and the reason they live in ONE function is that they did not. ``--out``
    applied all three; the ``<out>.previous`` path added later applied the first two and
    was reported as a defect for exactly the case the third one catches -- a directory of
    the operator's own regular files that happen to use bundle names. Each site is about to
    run a RECURSIVE DELETE, so a rule missing from one of them is data loss.

    1. NAMES: nothing at the top level this build does not write.
    2. SHAPES: nothing anywhere that is not a plain file or directory. The name rule reads
       the CONTAINER while the delete is recursive, so ``skills`` being an owned name let
       ``skills/notes.txt`` through, and ``p.is_file()`` was False for an empty directory,
       a FIFO, a socket and a link to a directory -- each invisible, then deleted.
    3. THE MANIFEST'S OWN DIGEST: names and shapes are both satisfied by a directory
       someone else assembled. A bundle this build wrote carries a manifest whose digest
       covers every file except the manifest, and the plan is written after that digest is
       taken, so re-deriving while skipping the plan reproduces the recorded value exactly
       when nothing has been added, moved or edited.

    ``flag`` names the path in the operator's own vocabulary, so the message points at
    something they can act on rather than at an internal name.
    """
    if _is_redirecting_entry(d):
        # The ANCHOR, before anything relative to it. ``d.exists()``/``is_dir()``/``iterdir()``
        # and ``bundle_digest(d)`` below all FOLLOW a symlinked or junctioned ``d``, so a
        # redirected root would have its TARGET verified for ownership and then the recursive
        # delete keyed to this verdict would run through the link -- the tree under the anchor
        # was checked while the anchor itself was not. Refuse the root first: everything else
        # in this function is relative to it, and a verdict about a root you did not verify is
        # a verdict about the wrong tree.
        raise ExportRefused(
            f"{flag} {d} is a symlink or reparse point. Its ownership cannot be verified "
            f"because every check here would follow it to another tree, and a recursive "
            f"delete keyed to that verdict would run through the link. Point {flag} at a real "
            f"directory."
        )
    if d.exists() and not d.is_dir():
        raise ExportRefused(
            f"{flag} {d} exists and is not a directory. `exists()` is true for a plain "
            f"file and the scans below would then raise instead of refusing. Move that "
            f"file, or point --out elsewhere."
        )
    # ``iterdir`` on an unreadable existing ``d`` raises ``PermissionError``, which is NOT an
    # ``ExportRefused``; every caller keys its staging/marker cleanup to ``ExportRefused``, so
    # a raw ``OSError`` escaping here skips that cleanup and leaks the staging tree and its
    # ownership marker -- and the marker is what authorises the next run's recursive delete.
    # "Unreadable" is refused, in the same category as "not owned", not left to crash: convert
    # the enumeration failure into ``ExportRefused`` so the existing cleanup runs.
    try:
        strangers = sorted(p.name for p in d.iterdir() if p.name not in _STAGING_OWNED_TOP_LEVEL)
    except OSError as exc:
        raise ExportRefused(
            f"{flag} {d} exists but could not be listed ({exc}); refusing rather than leave "
            f"it unverified. Fix its permissions or point {flag} elsewhere."
        ) from exc
    if strangers:
        raise ExportRefused(
            f"{flag} {d} holds files this build does not own "
            f"({', '.join(strangers[:5])}"
            + (f", and {len(strangers) - 5} more" if len(strangers) > 5 else "")
            + "). Building replaces the whole directory, so it would delete them. "
            "Point --out at a fresh or previous bundle directory."
        )
    wrong_shape = sorted(
        p.relative_to(d).as_posix()
        for p in _walk_no_reparse(d)
        if _is_shape_this_build_never_writes(p)
    )
    if wrong_shape:
        raise ExportRefused(
            f"{flag} {d} holds entries of a shape this build never writes "
            f"({', '.join(wrong_shape[:5])}"
            + (f", and {len(wrong_shape) - 5} more" if len(wrong_shape) > 5 else "")
            + "). Building replaces the whole directory, so it would delete them, and a "
            "link, a FIFO or a device node is not something a previous bundle left "
            "behind. Point --out at a fresh or previous bundle directory."
        )
    entries = [p for p in _walk_no_reparse(d) if p.is_file()]
    # DIRECTORIES are verified too, by whether they lead anywhere this build wrote.
    #
    # Every check above this line either looks at the top level only (``d.iterdir()``) or at
    # SHAPE, and a plain directory passes both. ``entries`` then filters to ``is_file()``, so
    # a directory was never compared against anything at all: ``<out>/skills/notes/`` -- an
    # operator's own empty directory under a name this build does write -- passed the whole
    # scan and was removed by the ``rmtree`` below. The digest check could not catch it
    # either, because a digest is taken over file content and an empty directory contributes
    # none.
    #
    # A directory this build produced has a file under it, with ONE exception measured here:
    # ``skills/`` is created even when the plan selects no skills, so the top-level names this
    # build writes are owned whether or not anything is under them. Below that level the rule
    # holds, and below that level is where the loss was: ``<out>/skills/notes/``.
    owned_dir_paths = {parent for p in entries for parent in p.relative_to(d).parents}
    empty_dirs = sorted(
        rel.as_posix()
        for rel in (
            p.relative_to(d) for p in _walk_no_reparse(d) if p.is_dir() and not p.is_symlink()
        )
        if rel not in owned_dir_paths and rel.as_posix() not in _BUILD_WRITES_EMPTY
    )
    if empty_dirs:
        raise ExportRefused(
            f"{flag} {d} holds directories with no file this build would have written "
            f"({', '.join(empty_dirs[:5])}"
            + (f", and {len(empty_dirs) - 5} more" if len(empty_dirs) > 5 else "")
            + "). Building replaces the whole directory, so it would delete them, and an "
            "empty directory is not something a previous bundle left behind. Point --out at "
            "a fresh or previous bundle directory."
        )
    non_plan = [p for p in entries if p.relative_to(d).as_posix() != PLAN_FILENAME]
    manifest_path = d / "manifest.json"
    if not non_plan and entries:
        # A directory holding ONLY the plan file is the normal state between the `plan`
        # verb and the `build` verb, so it must be accepted -- refusing it would break the
        # documented two-step workflow. What is checked instead is that the plan is one
        # THIS tool wrote: the previous code accepted the directory on the FILENAME alone,
        # so a directory whose single file happened to be called curation-plan.json was
        # deleted recursively without anything looking inside it.
        try:
            body = json.loads((d / PLAN_FILENAME).read_text(encoding="utf-8"))
            recognised = isinstance(body, dict) and body.get("plan_version") == PLAN_VERSION
        except (OSError, ValueError):
            recognised = False
        if not recognised:
            raise ExportRefused(
                f"{flag} {d} holds a single {PLAN_FILENAME} that this tool did not write "
                f"(no plan_version {PLAN_VERSION}). The name alone is not proof of origin, "
                f"and building replaces the directory recursively. Point --out at a fresh "
                f"directory or at a complete previous bundle."
            )
    if non_plan and not manifest_path.is_file():
        raise ExportRefused(
            f"{flag} {d} has bundle-shaped contents but no manifest.json, so it is not a "
            "directory this build produced and replacing it would delete files of "
            "unknown origin. Point --out at a fresh directory or at a complete previous "
            "bundle."
        )
    if non_plan:
        try:
            decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(decoded, dict):
                # A manifest that PARSES but is not an object: ``[]`` decodes fine and then
                # ``.get`` raises AttributeError, which is not in the tuple below. Measured: a
                # rebuild over such a bundle exited as a traceback, and it happens after the
                # staging tree and its ownership marker exist, so the operator is left with
                # both and no message naming either.
                raise ExportRefused(
                    f"{flag} {d} has a manifest.json that decodes to "
                    f"{type(decoded).__name__}, not an object, so the bundle it claims to "
                    f"describe cannot be verified before a recursive replace."
                )
            recorded = decoded.get("digest")
        except (OSError, ValueError) as exc:
            raise ExportRefused(
                f"{flag} {d} has a manifest.json that cannot be read ({exc}), so the "
                "bundle it claims to describe cannot be verified before a recursive "
                "replace."
            ) from None
        if recorded != bundle_digest(d, also_skip=frozenset({PLAN_FILENAME})):
            raise ExportRefused(
                f"{flag} {d} does not match the bundle its manifest describes, so it "
                "holds at least one file this build did not write (a nested stray such "
                "as skills/notes.txt, or an edited file). Building replaces the "
                "directory recursively and would delete it. Point --out at a fresh "
                "directory."
            )


def _dispose_via_private_aside(
    target: Path, verify: Callable[[Path], None], settle: Callable[[Path], None]
) -> None:
    """Recursively delete ``target`` through a run-private aside, not by re-resolving its path.

    ``shutil.rmtree(target)`` re-resolves ``target`` from its path string, so verifying the
    ownership of ``target`` at its path only NARROWS the window -- a swap between the check
    and rmtree's own resolution still lands the recursive delete on whatever the path names
    then, and that delete is irreversible. Verifying ``target`` at its path BEFORE the rename
    has the same window in the other order: what the rename then captures need not be what was
    verified. This removes the window by binding the two to one entry:

    1. Create a private directory beside ``target`` with ``mkdir`` (``O_EXCL`` semantics via
       ``exist_ok`` False) and mode ``0o700`` -- this build is the only writer of a name no
       other process chose, so nothing can pre-plant or swap it.
    2. ``os.rename`` ``target`` into that private directory. ``rename`` acts on the entry, not
       a re-resolved path: it moves whatever ``target`` IS at that instant into a directory
       only this build can reach. A concurrent swap either loses the rename race (``target``
       already gone) or moves the swapped tree into the private directory, where nothing
       outside can be reached.
    3. ``verify`` the MOVED tree -- the exact entry the rename captured, now at a path no other
       writer holds and so unswappable. If it is not one this build wrote, rename it BACK to
       where it came from (a swapped-in tree the operator owns is returned untouched) and
       refuse; only a verified tree is deleted.
    4. ``rmtree`` the private directory. Every path deleted is under a root no other writer
       holds, so the recursive delete cannot be redirected outside it, and it is the same
       inode step 3 verified.

    Best-effort by design at the edges: if ``target`` is already gone (step 2 raises
    ``FileNotFoundError``) there is nothing to delete and the private dir is removed; a
    partially-created private dir is cleaned on any failure.
    """
    parent = target.parent
    private = parent / f".smc-purge-{uuid.uuid4().hex}"
    private.mkdir(mode=0o700)  # exist_ok False: we alone create this exact name
    cleanup_private = True
    try:
        moved = private / target.name
        try:
            os.rename(target, moved)
        except FileNotFoundError:
            # target vanished (a concurrent process removed or moved it first); nothing to
            # delete, and the empty private dir is cleaned in the finally below.
            return
        try:
            verify(moved)
        except ExportRefused:
            # The entry the rename captured is not one this build wrote -- a tree swapped in
            # before the rename won the race. Try to return it to where it came from, then
            # refuse WITHOUT deleting it. A failed restore is not a licence to delete a tree
            # this build did not put there: if the rename-back fails, RETAIN the private aside
            # (do not let the finally rmtree it) and name where the tree now sits, so nothing
            # recursively deletes an operator-owned tree. There is no correct recursive delete
            # of a tree we did not create.
            try:
                os.rename(moved, target)
            except OSError as restore_exc:
                cleanup_private = False
                raise ExportRefused(
                    f"the aside path was replaced by a tree this build did not write, and "
                    f"restoring it to {target} failed ({restore_exc}). It has NOT been deleted "
                    f"-- it is at {moved}. Nothing was removed; move it back or remove it by "
                    f"hand."
                ) from restore_exc
            raise
        # Disposal is the caller's, because only the caller knows what a verified tree is FOR:
        # the previous bundle is deleted, the operator's current one is kept as the rollback
        # copy. What must not vary is which entry the disposal acts on -- the one the rename
        # captured and ``verify`` just cleared, never a path resolved again.
        try:
            settle(moved)
        except BaseException:
            # Disposal raised, and the MOVED tree is still in the private aside -- for the
            # ``os.rename(moved, previous)`` settle this is the operator's current bundle,
            # verified moments ago. The finally below would recursively delete it. Same
            # discipline as the verify-failure path above: put it back where it came from, and
            # if that cannot be done, RETAIN the aside and name where the tree sits rather than
            # deleting a tree this build did not create. ``BaseException`` because the obligation
            # not to delete the operator's tree holds regardless of why disposal failed -- a
            # cancelled build included -- and it re-raises, so nothing is swallowed.
            try:
                os.rename(moved, target)
            except OSError as restore_exc:
                cleanup_private = False
                raise ExportRefused(
                    f"the tree at {target} was moved aside, disposing of it failed, and "
                    f"restoring it failed too ({restore_exc}). It has NOT been deleted -- it "
                    f"is at {moved}. Move it back or remove it by hand."
                ) from restore_exc
            raise
    finally:
        if cleanup_private:
            shutil.rmtree(private, ignore_errors=True)


def _purge_via_private_aside(target: Path, verify: Callable[[Path], None]) -> None:
    """Delete ``target`` through the private aside: capture, verify, then remove."""
    _dispose_via_private_aside(
        target, verify, lambda moved: shutil.rmtree(moved, ignore_errors=True)
    )


def _publish_report(report_tmp: Path, report_path: Path, report_before: "bytes | None") -> None:
    """Atomically move ``report_tmp`` onto ``report_path``, bound to one parent descriptor.

    ``os.replace(report_tmp, report_path)`` re-resolves ``report_path`` by NAME, so a concurrent
    process that drops a foreign file there between the caller's shape checks and this replace
    would have that file clobbered -- "I chose this path" is not "I own what is at it now". This
    opens the parent once with ``O_NOFOLLOW | O_DIRECTORY`` and re-checks the leaf by ``lstat``
    against that descriptor immediately before the replace, so the entry verified and the entry
    replaced are reached through one descriptor no concurrent rename of the parent can redirect.

    Shape is not the whole of ownership. A value read back has four independent properties, and
    each can have changed since we last saw it: whether it EXISTS, whether it is the SAME OBJECT,
    whether its CONTENT is unchanged, and whether it is READABLE. The shape ``lstat`` covers the
    first two; a concurrent process that edits the report IN PLACE leaves the same object, still
    readable, with different bytes -- missing none of the first two, so a shape check alone says
    fine and ``os.replace`` destroys that edit silently. The build owns the report exclusively
    for the duration of one build (it only ever writes it through ``report_tmp`` + this replace,
    never in place), so the bytes at ``report_path`` must still equal what the caller read before
    the build (``report_before``), or the file must be absent. Anything else is a foreign edit,
    and the only definitely-wrong answer is to overwrite it -- a report is not mergeable, so drift
    is REFUSED. The content is read through the SAME descriptor the replace targets, so the bytes
    compared are the bytes that would be clobbered.

    Consults ``_dir_fd_supported`` for the same reason every ``O_DIRECTORY`` user does: on a
    platform without descriptor-relative opens there is no atomic form, and the whole builder
    already refuses on such a platform before reaching here -- but the guard is stated locally
    so the rule that every ``O_DIRECTORY`` use is gated holds by reading, not by trust.
    """
    if not _dir_fd_supported():
        # Unreachable in practice (the builder refuses at its entry on such a platform), but a
        # by-name replace here would be the very window this helper closes, so refuse rather
        # than silently take it.
        raise ExportRefused(
            "cannot publish the report atomically without descriptor-relative opens on this "
            "platform; the builder is POSIX-only until that primitive exists."
        )
    try:
        parent_fd = _open_dir_nofollow_pinned(report_path.parent)
    except OSError as exc:
        # Pin every component of the report's parent, not just the leaf: opening the parent by
        # bare path string re-resolved it and followed a grandparent/intermediate swapped into
        # the window, after which the lstat, the content re-read, and the os.replace below all
        # run relative to a descriptor pointing outside --out. A component swapped after
        # resolution fails its own no-follow open and arrives here as a refusal.
        raise ExportRefused(
            f"cannot publish the report at {report_path}: a component of its directory is "
            f"not there, is not a directory this build can open, or changed to a link "
            f"({exc}). The path is derived from --out; point --out elsewhere."
        ) from exc
    try:
        try:
            st = os.lstat(report_path.name, dir_fd=parent_fd)
        except FileNotFoundError:
            st = None
        if st is not None and not stat.S_ISREG(st.st_mode):
            raise ExportRefused(
                f"{report_path} is not a plain file at publish time (it was replaced by "
                f"another object during the build). Refusing to overwrite it; point --out "
                f"elsewhere."
            )
        if st is not None:
            # Same object, still readable -- but is it the same CONTENT the caller read before
            # the build? Read it back through the SAME descriptor the replace will target
            # (no-follow, so a leaf swapped to a link is refused by the open, not chased), and
            # refuse if the bytes drifted: that is a concurrent in-place editor whose write
            # ``os.replace`` would otherwise destroy without a trace.
            leaf_fd = os.open(
                report_path.name, os.O_RDONLY | _NOFOLLOW_READ_FLAGS, dir_fd=parent_fd
            )
            try:
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(leaf_fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                current = b"".join(chunks)
            finally:
                os.close(leaf_fd)
            if current != report_before:
                raise ExportRefused(
                    f"{report_path} was edited by another process while this build ran "
                    f"(its bytes changed since the build started). The report is written "
                    f"only through an atomic replace, so an in-place change is a foreign "
                    f"edit; refusing to overwrite it rather than destroy that write. "
                    f"Re-run the build once nothing else is writing there."
                )
        os.replace(report_tmp, report_path.name, dst_dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def build_bundle(
    crew: ResolvedCrew,
    agent_spec: dict,
    candidates: dict[str, list[Candidate]],
    plan: Plan | None,
    out_dir: Path,
) -> BuildReport:
    """Write the four-entry bundle for *crew*, or refuse and leave nothing behind.

    "Leave nothing behind" holds for every refusal BEFORE promotion: the staging tree, its
    marker, and any temp report are cleaned and the prior bundle is left in place. There is one
    deliberate exception AFTER promotion. Publication is ordered promotion first
    (``staging.rename(out_dir)``), then the report, because a report written before a promotion
    that then fails would be a false success claim in the one artifact an operator reads as
    proof -- and a MISSING report is recoverable by regenerating where a FALSE one is not. So if
    the report publish fails after a good promotion, the NEW bundle stays installed and the
    report is absent: a partial success, not a clean refusal. This is the strictly-better of the
    two, and it is the only state in which this function returns having neither fully succeeded
    nor left nothing behind.
    """
    _refuse_without_nofollow_primitive()
    _refuse_unc_out(out_dir)
    included_mcp = plan.included("mcp") if plan else set()
    included_skills = plan.included("skills") if plan else set()

    result = build_spec(crew, agent_spec, included_mcp, crew.agent_spec_path.parent)

    # The PARENT is judged first, before any of the three derived paths below exist as
    # names. Every one of them -- the staging tree, its marker, the report -- is
    # ``out_dir.parent / <something>``, so a junction at that parent silently relocates all
    # of them together, and the per-path checks further down each validate a path that is
    # already pointing somewhere else. Guarding one derived path at a time cannot catch a
    # redirect in the component they share.
    _refuse_unusable_parent(out_dir, what="the bundle")
    staging = out_dir.parent / (out_dir.name + ".staging")
    # Beside staging, not inside: see the marker note below. Cleaned on every exit path,
    # because a marker left behind is a licence for the NEXT run to delete whatever sits at
    # that path.
    staging_marker = out_dir.parent / (out_dir.name + ".staging.owned")
    # A PLAIN FILE at either path is refused before any directory call. `exists()` is true
    # for a file, so `_walk_no_reparse(staging)` and `out_dir.iterdir()` below both raised an
    # uncaught NotADirectoryError -- reproduced for each -- and the staging directory was
    # left on disk by the crash. A refusal is the same answer the residue checks give, and
    # it arrives before anything is created.
    # ``is_symlink`` FIRST at both paths, because ``is_dir()`` follows links and so answers
    # about the target rather than the entry.
    #
    # Measured, rather than assumed: with a link at ``--out`` pointing at a directory, the
    # build SUCCEEDS and the promotion replaces the link with a real directory. The target
    # does not receive the bundle and is left orphaned, so an operator who arranged that link
    # deliberately -- pointing ``--out`` at a volume, a share, a versioned directory -- loses
    # the arrangement silently, and anything else reading through the target keeps stale
    # content while the path they published now serves the new bundle.
    #
    # The existing stranger check catches SOME of these by accident, because a target holding
    # the operator's own files trips "holds files this build does not own". It says nothing
    # when the target is empty or holds a valid previous bundle, which are the ordinary cases
    # for a deliberately placed link.
    for label, candidate in (("the staging path", staging), ("--out", out_dir)):
        if _is_redirecting_entry(candidate):
            raise ExportRefused(
                f"{label} {candidate} is a symlink. Promotion replaces that path with a real "
                f"directory, so building here would destroy the link and orphan whatever it "
                f"points at. Point --out at a real directory."
            )
    if staging.exists() and not staging.is_dir():
        raise ExportRefused(
            f"the staging path {staging} exists and is not a directory. It is derived from "
            f"--out by appending '.staging', so --out is pointing somewhere this build "
            f"cannot work. Move that file, or point --out elsewhere."
        )
    if out_dir.exists() and not out_dir.is_dir():
        raise ExportRefused(
            f"--out {out_dir} exists and is not a directory. A bundle is four entries in a "
            f"directory, so this cannot be replaced in place. Point --out at a fresh "
            f"directory or at a complete previous bundle."
        )
    # Whether an existing marker is one WE wrote. Computed here, before anything is
    # created, and passed to the write below: it is the only ownership proof in this
    # function, and the write must not decide for itself whether to remove what is there.
    marker_is_ours = _marker_is_ours(staging_marker)
    if staging.exists():
        # PROOF that this build made it, not a description of what is inside. The name and
        # shape rules were here first and both are satisfied by an operator's own
        # directory: `skills` is a name this build writes, so `<out>.staging/skills/notes.txt`
        # passed the top-level check and the recursive delete then removed notes.txt.
        #
        # The digest rule the other two sites use cannot apply here. Staging is filled in
        # incrementally and its manifest is written near the end, so a crashed staging
        # directory legitimately has no digest to verify -- checking one would refuse
        # exactly the case this branch exists to clean up.
        #
        # So the marker. This build CREATES staging, so it can leave a token saying so, and
        # a directory without one was made by someone else whatever it contains. It sits
        # BESIDE staging rather than inside: `bundle_digest(staging)` is a frozen contract
        # value computed over everything in there, so a file inside would either change
        # that digest or ship inside the bundle.
        if not marker_is_ours:
            raise ExportRefused(
                f"the staging path {staging} already exists and this build did not create "
                f"it (no {staging_marker.name} beside it carrying this builder's marker). "
                f"It is derived from --out by appending '.staging', and building would "
                f"delete it recursively. Move it, or point --out elsewhere."
            )
        # It IS ours, so the older content rules still apply: they catch a staging directory
        # this build made and something else then wrote into.
        residue = sorted(
            p.relative_to(staging).as_posix()
            for p in _walk_no_reparse(staging)
            if p.relative_to(staging).parts[0] not in _STAGING_OWNED_TOP_LEVEL
            or _is_shape_this_build_never_writes(p)
        )
        if residue:
            raise ExportRefused(
                f"the staging path {staging} already holds files this build did not "
                f"write ({', '.join(residue[:5])}"
                + (f", and {len(residue) - 5} more" if len(residue) > 5 else "")
                + "). It is derived from --out by appending '.staging', and building "
                "would delete it recursively. Move it, or point --out elsewhere."
            )
        shutil.rmtree(staging)
    # The marker path's SHAPE is judged before staging is created, for the reason stated
    # above about a plain file at either path: a refusal that arrives after ``mkdir`` leaves
    # a staging tree nothing cleans up, so the operator gets a traceback and a directory to
    # remove by hand. ``_write_nofollow`` refuses a directory here, and this is where that
    # refusal has to happen for it to cost nothing.
    if staging_marker.is_dir() and not staging_marker.is_symlink():
        raise ExportRefused(
            f"{staging_marker} is a directory. This build needs that exact path for its "
            f"staging marker, and it will not delete a directory to get it. It is derived "
            f"from --out by appending '.staging.owned'. Move it, or point --out elsewhere."
        )
    # ``exist_ok`` stays FALSE: creating the directory is how this build CLAIMS the staging
    # path, and succeeding when it already exists would put two builds in one tree.
    #
    # A ``FileExistsError`` here is the concurrent-claim loser: two builds cleared preflight
    # for the same ``--out`` and both reached this line; the winner created staging, this one
    # lost the race. It has created nothing yet, so there is no partial state to unwind --
    # translate the crash into a clean refusal so the loser gets an "already claimed" outcome
    # instead of a traceback. The pre-mkdir checks above refuse a PRE-EXISTING staging tree
    # (link, non-directory, unowned, or holding files) with a better message; this covers
    # only the narrow window between those checks and this create.
    try:
        staging.mkdir(parents=True)
    except FileExistsError:
        raise ExportRefused(
            f"the staging path {staging} was claimed by another build in progress. "
            f"One build owns a given --out at a time; re-run once the other finishes."
        )
    try:
        _write_marker_exclusive(staging_marker, ours=marker_is_ours)
    except BaseException:
        # The marker write can refuse a pre-existing foreign or redirecting marker, and it
        # runs AFTER the mkdir above. Without this, that refusal leaves the staging tree
        # behind, and the pre-mkdir checks then read it as another build's claim -- so the
        # first refusal makes every later run refuse too, for a different reason, until
        # someone deletes the directory by hand. Only the tree THIS call created is removed.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # The swap below replaces out_dir wholesale, which is what makes a failed build
    # leave nothing half-written. But the plan command writes its review template
    # INTO this same directory, so the documented flow (plan, sign, build with the
    # same --out) had the build delete the signed plan it had just read, with no
    # message. The owner then had to regenerate and re-sign without being told why.
    #
    # Two rules, so the atomic swap survives without eating anything:
    #   1. Refuse when out_dir holds something this build does not own. Pointing
    #      --out at a directory of unrelated files is exactly when a silent
    #      recursive delete does the most damage, so it is refused by name rather
    #      than absorbed.
    #   2. Carry the plan through the staging directory, so it lands back in the
    #      new out_dir instead of being replaced along with the bundle.
    carried_plan: bytes | None = None
    # Declared BEFORE the try, because the handler reads it. Bound inside, it would be
    # unbound for every failure that happens earlier in the block -- and the handler runs
    # on exactly those, so the restore would raise NameError and mask the real error.
    previous: Path | None = None

    # Established BEFORE the try, because the except block reads all three and a refusal
    # raised early in the body would otherwise hit UnboundLocalError -- which does not just
    # lose the rollback, it REPLACES the real refusal with a confusing one. Found exactly
    # that way: 13 tests turned red naming UnboundLocalError instead of the ExportRefused
    # they assert.
    #
    # The report is written before the swap on purpose -- a report failure must not land
    # after the previous bundle is gone -- and that ordering is what leaves the other hole:
    # a rename failure restores the previous bundle while the report still describes the new
    # one that never landed. The transaction has to cover both files or it covers neither.
    report_path = out_dir.parent / f"{out_dir.name}.smc-bundle.json"
    report_before: bytes | None = None
    if report_path.is_file() and not _is_redirecting_entry(report_path):
        # Fail closed rather than treat an unreadable existing report as absence. On the
        # rollback path below, ``report_before is None`` means "no report was here, so unlink
        # the one this run wrote" -- if a read failure quietly set it to None, a rollback
        # would DELETE the operator's existing report instead of restoring it. The read is
        # the only thing that tells "no report" from "a report we could not read".
        # Read the baseline through the whole-window no-follow reader, not ``read_bytes``,
        # which follows every component: a parent/intermediate swapped after the leaf check
        # above would be traversed and the drift/rollback baseline taken from outside --out.
        # Inside this ``is_file()`` branch a ``None`` return means unreadable or redirected,
        # never absent, so it fails closed the same way the old ``OSError`` branch did.
        report_before = _read_bytes_openat(report_path.parent, Path(report_path.name))
        if report_before is None:
            # Release the staging tree and marker this build already created before refusing.
            # This refusal sits BEFORE the main transaction's own cleanup, so without this the
            # correct refusal would leak the tree and -- worse -- the ownership marker, which
            # the next run reads as another build's claim and refuses on, turning one refusal
            # into a standing one until someone deletes the directory by hand. A refusal must
            # release what this build acquired, not only report the reason.
            shutil.rmtree(staging, ignore_errors=True)
            staging_marker.unlink(missing_ok=True)
            raise ExportRefused(
                f"the existing report at {report_path} cannot be read or a component of its "
                f"path changed to a link, so this build cannot restore it if the swap fails "
                f"and will not risk deleting it. Fix or remove that file."
            )
    report_written = False
    promoted = False
    report_tmp = report_path.parent / (report_path.name + f".{_RUN_ID}.tmp")
    if out_dir.exists():
        # The SAME vocabulary the staging check above uses. It was briefly written
        # out twice, which is the duplicate-spelling mistake this branch has paid for
        # more than once: two copies of one rule drift, and here the drift would be
        # one of the two recursive deletes quietly accepting a name the other
        # refuses.
        # One function owns all three rules (names, shapes, the manifest's own digest),
        # because this site had all three and the `<out>.previous` site below had only the
        # first two -- reported as a defect for precisely the case the third one catches.
        # Both are about to run a recursive delete, so they cannot be allowed to drift.
        try:
            _refuse_unless_this_build_wrote_it(out_dir, "--out")
        except ExportRefused:
            shutil.rmtree(staging, ignore_errors=True)
            staging_marker.unlink(missing_ok=True)
            raise
        plan_file = out_dir / PLAN_FILENAME
        if plan_file.is_file():
            # Inside the cleanup transaction, and translated. This read sat OUTSIDE the
            # ``except ExportRefused`` above, so an unreadable plan -- a permission change, a
            # file that became a directory, a device node -- raised a bare OSError past every
            # handler and left the staging tree and its marker on disk. The marker is worse
            # than the tree: it is what authorises the NEXT run's recursive delete.
            carried_plan = _read_bytes_openat(out_dir, Path(PLAN_FILENAME))
            if carried_plan is None:
                shutil.rmtree(staging, ignore_errors=True)
                staging_marker.unlink(missing_ok=True)
                raise ExportRefused(
                    f"the existing plan at {plan_file} cannot be read or a component of its "
                    f"path changed to a link, so this build cannot carry it across the swap "
                    f"and will not replace the bundle without it. Fix or remove that file."
                )

    try:
        _write_guarded(
            staging / "agent.json",
            json.dumps(result.spec, indent=2, ensure_ascii=False) + "\n",
            "agent.json",
        )
        _write_guarded(
            staging / "mcp.json",
            json.dumps({"mcpServers": result.mcp}, indent=2, ensure_ascii=False) + "\n",
            "mcp.json",
        )
        skills_dst = staging / "skills"
        skills_dst.mkdir(exist_ok=True)  # MUST exist even when empty
        for cid in sorted(included_skills):
            skill_dir = crew.skills_root / cid
            # ``is_dir()`` follows, so a selected skill replaced by a junction between the
            # review and this copy would answer True and be copied THROUGH to its target.
            # Checked by ``lstat`` first: the pin recheck below compares the staged bytes to
            # the reviewed hash, but a redirect that names a share has already been probed by
            # then, and on Windows that probe is the credential exchange.
            if _is_redirecting_entry(skill_dir):
                raise ExportRefused(
                    f"selected skill {cid} is a link or a reparse point, so copying it would "
                    f"take bytes from wherever it points rather than from the crew."
                )
            if not skill_dir.is_dir():
                raise ExportRefused(f"selected skill has gone: {cid}")
            written = _copy_skill(skill_dir, cid, skills_dst, included_skills)
            # Re-hash the STAGED copy against the reviewed pin. ``verify()`` compared
            # the pin to a hash taken at ENUMERATION time, and this copy reads the
            # source directory again -- two moments, with the source writable in
            # between. Losing that race would put bytes nobody reviewed into a signed
            # bundle, which is the one thing the signature is supposed to prevent.
            #
            # Hashing the copy rather than re-reading the source is what makes this
            # closed rather than merely narrower: what the source says afterwards does
            # not matter, because what is checked is the artifact that ships.
            #
            # A MISSING pin is deliberately not re-refused here. ``verify()`` already
            # owns that refusal, and spelling it twice is the duplicate-check mistake
            # this branch has already paid for elsewhere -- it also changed the
            # outcome of the deny-by-default mutation test, which probes exactly this
            # path with pins absent.
            reviewed = plan.pins.get("skills", {}).get(cid, "") if plan else ""
            if reviewed:
                staged = _staged_tree_hash(skills_dst / cid, skill_dir, written)
                if staged != reviewed:
                    raise ExportRefused(
                        f"skills/{cid} changed while the bundle was being written, so "
                        f"the copy that would ship is not the copy that was approved."
                        f"\n  reviewed: {reviewed}\n  staged:   {staged}\n"
                        f"Re-run the plan command and look again."
                    )

        digest = bundle_digest(staging)
        _write_guarded(
            staging / "manifest.json",
            json.dumps(
                {
                    "bundle_version": BUNDLE_VERSION,
                    "crew_name": crew.name,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "digest": digest,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            "manifest.json",
        )
        # The previous bundle is MOVED ASIDE, not deleted. `rmtree(out_dir)` followed by
        # `staging.rename(out_dir)` is two operations, and a failure between them left
        # NOTHING: the old bundle was already gone, and the `except BaseException` below
        # then deleted staging too, taking the new bundle and the carried plan with it.
        # The comment above this claimed the swap was "the last thing that happens" --
        # true of the ordering, false of the atomicity, which is the kind of comment that
        # stops anyone from looking.
        #
        if carried_plan is not None:
            # AFTER the digest, deliberately, and the review that asked for the opposite is
            # answered here rather than in a comment thread.
            #
            # The plan is the OPERATOR's file. ``_cmd_plan`` writes it into --out, the
            # operator edits and signs it, and the next build carries it forward -- so it is
            # expected to differ between builds, which is what
            # ``test_the_plan_flow_still_works`` pins by editing it and rebuilding. Putting it
            # inside the digest makes every such edit break the rebuild preflight: measured,
            # that change reddened that test and one more.
            #
            # And it protects nothing, because nothing reads it. The container consumes four
            # entries -- manifest.json, agent.json, mcp.json, skills/ (``BUNDLE_ENTRIES``) --
            # and ``crew/runtime/**`` contains no reference to the plan filename at all. What
            # ships was decided at build time and is covered by the digest; the plan beside it
            # is a record for the humans, living in that directory for convenience.
            #
            # Into staging rather than back into out_dir after the rename: the swap stays the
            # last thing that happens, so a failure above leaves the existing directory and
            # its plan untouched.
            # Re-read before writing back, and refuse if it changed. The bytes above were
            # taken before the build ran, so an operator who edited and re-signed the plan
            # while it ran would have that edit silently replaced by the stale copy -- and
            # the plan is THEIR file, the one they sign. Refusing costs them a rebuild;
            # overwriting costs them a signature they have to reproduce without being told
            # it was lost.
            current_plan = _read_bytes_openat(out_dir, Path(PLAN_FILENAME))
            if current_plan is None:
                # Fail closed rather than skip the concurrent-edit guard. If this read failed
                # and we treated it as carried, the guard below would be bypassed and
                # ``carried_plan`` -- the stale copy read at the start -- would be written over
                # the operator's signed plan. An unreadable-or-redirected plan at write-back
                # time is exactly when we must NOT write, so refuse and leave their file alone.
                shutil.rmtree(staging, ignore_errors=True)
                staging_marker.unlink(missing_ok=True)
                raise ExportRefused(
                    f"{plan_file} could not be re-read before carrying it across the swap "
                    f"(unreadable, or a component of its path changed to a link), so this "
                    f"build cannot confirm it is unchanged and will not risk overwriting it "
                    f"with the copy read at the start. Nothing was installed and the existing "
                    f"bundle is untouched. Re-run the build."
                )
            if current_plan != carried_plan:
                shutil.rmtree(staging, ignore_errors=True)
                staging_marker.unlink(missing_ok=True)
                raise ExportRefused(
                    f"{plan_file} changed while this build was running, so carrying the "
                    f"copy read at the start would discard that edit. Nothing was "
                    f"installed and the existing bundle is untouched. Re-run the build to "
                    f"pick up the current plan."
                )
            # No-follow, like every other staged leaf: the staging tree lives beside --out in a
            # directory this build does not own, so a same-UID process can plant a symlink at
            # this leaf in the window after ``staging.mkdir`` and a following ``write_bytes``
            # would truncate whatever the link named and ship a redirect as the plan. Written
            # through the bytes no-follow primitive so a link at the leaf is refused at open,
            # and the signed plan lands byte-for-byte.
            _write_bytes_nofollow(staging / PLAN_FILENAME, carried_plan)
        # A rename within one directory is atomic, so at every instant either the old
        # bundle or the new one is at out_dir, and the aside copy is deleted only after
        # the new one is in place.
        if out_dir.exists():
            previous = out_dir.parent / (out_dir.name + ".previous")
            if _is_redirecting_entry(previous):
                # Before ``exists()``, which follows the link. This path is derived from
                # --out, so a redirect here aims the ownership check and the rmtree below it
                # at somewhere else entirely -- and the check would pass, because it would be
                # examining whatever the link points at. The same fix landed at ``staging``
                # and ``out_dir`` last round and this third derived path did not get it.
                raise ExportRefused(
                    f"the aside path {previous} is a link or junction. The previous bundle is "
                    f"moved there and then deleted, so following a redirect would delete "
                    f"somewhere this build was never pointed at. Remove it, or point --out "
                    f"elsewhere."
                )
            if previous.exists():
                # The SAME three rules --out gets, from the same function. This path is
                # derived from --out, so `<out>.previous` can be a directory the operator
                # put there themselves -- and one holding their own regular files under
                # bundle names passed the earlier two-rule version of this check and was
                # deleted. The manifest digest is the rule that tells their directory from
                # one this build wrote.
                # Delete through a RUN-PRIVATE aside, and verify ownership on the MOVED tree
                # rather than at this path. A plain ``rmtree(previous)`` re-resolves the path
                # string, so even an identity check taken immediately before it leaves a
                # window; verifying at the path before the rename has the same window in the
                # other order, because what the rename then captures need not be what was
                # verified. Instead ``_purge_via_private_aside`` atomically ``rename``s
                # ``previous`` into a directory THIS build just created and owns exclusively,
                # then runs the ownership check on the entry the rename captured -- now at a
                # path no other writer holds and so unswappable -- and deletes only if it
                # passes, restoring a swapped-in operator tree untouched otherwise. The
                # verified inode and the deleted inode are one and the same.
                _purge_via_private_aside(
                    previous,
                    lambda moved: _refuse_unless_this_build_wrote_it(moved, "the aside path"),
                )
            # The same binding the aside path gets, for the same reason. ``out_dir`` was
            # verified as a tree this build wrote far above, and a rename here acts on
            # whatever the name IS by now: a tree swapped in between is moved to
            # ``previous`` unverified, the new bundle is then promoted over the original
            # path, and the ownership check that would have objected runs afterwards, when
            # the operator's data is already somewhere they did not put it. Capturing into
            # a run-private directory first makes the verified entry and the kept entry one
            # and the same, and a tree this build did not write is returned to where it came
            # from before anything is promoted.
            _dispose_via_private_aside(
                out_dir,
                lambda moved: _refuse_unless_this_build_wrote_it(moved, "--out"),
                lambda moved: os.rename(moved, previous),
            )
        # The report is written BEFORE the swap, which is the point of no return.
        #
        # Written here rather than by the caller after ``build_bundle`` returns -- and by then
        # this function had already renamed the previous bundle aside AND deleted it, so a
        # report write that failed left the operator with a non-zero exit code, no report, and
        # their previous bundle gone. A failure that has already replaced what it was going to
        # replace is the worst shape a failure can have.
        #
        # Everything the report says is known here: the digest was computed above, the
        # destination is out_dir, and the plan and candidates are arguments. So there is no
        # reason for it to happen later, and moving it up means a failure lands inside the
        # ``except BaseException`` below, which restores the previous bundle.
        # Written to a sibling temp and RENAMED over the destination, not written in
        # place. ``_write_nofollow`` opens with ``O_TRUNC``, so a write that fails partway
        # has already emptied the old report while ``report_written`` is still False and the
        # rollback below does not fire -- the one shape the rollback cannot see. A rename is
        # atomic within the directory, so the destination holds either the previous bytes or
        # the complete new ones and never a truncated mix.
        _write_nofollow(
            report_tmp,
            json.dumps(
                {
                    "report_version": REPORT_VERSION,
                    "crew_name": crew.name,
                    "bundle_dir": str(out_dir),
                    "digest": digest,
                    "skill_count": len(included_skills),
                    "mcp_servers": sorted(result.mcp),
                    "denied": _denied_list(candidates, plan),
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )
        # The DESTINATION's shape is judged here, because ``os.replace`` overwrites a
        # symlink rather than following it -- which is safe for the link's target but throws
        # away the refusal an in-place ``O_NOFOLLOW`` open gave. A planted link at the report
        # path must still be refused, and a rename alone cannot say so: it succeeds either
        # way. So the two properties are kept separately -- shape checked before, atomicity
        # by the rename after.
        if _is_redirecting_entry(report_path):
            raise ExportRefused(
                f"{report_path} is a link or junction. The report is written at a path "
                f"derived from --out, and os.replace would swap the link itself for a real "
                f"file -- destroying the link and orphaning whatever it named. Move it, or "
                f"point --out elsewhere."
            )
        if report_path.exists() and not report_path.is_file():
            raise ExportRefused(
                f"{report_path} exists and is not a plain file, so the report cannot "
                f"replace it. It is derived from --out; point --out elsewhere."
            )
        # Content drift is judged BEFORE promotion, not only inside ``_publish_report``. The
        # report is one of the values this build wrote and reads back, and "same object, still
        # readable" is not "same content": a concurrent process that edits it in place leaves a
        # readable regular file with different bytes, which the shape checks above pass. The
        # build owns the report exclusively for one build (it writes it only through the atomic
        # replace, never in place), so its bytes must still equal what was read at the start
        # (``report_before``) or be absent. A mismatch is a foreign edit, and it is refused HERE
        # -- before ``staging.rename`` -- because refusing after promotion is too late: the
        # rollback's "promoted and not report_written" branch would then UNLINK the report,
        # destroying the very edit this guard exists to protect. Refusing before promotion
        # leaves the prior bundle restored and the foreign report untouched. ``_publish_report``
        # repeats the check descriptor-relative to close the window between here and the replace.
        if report_before is not None and report_path.is_file():
            if _read_text_nofollow(report_path) != report_before.decode("utf-8", errors="replace"):
                raise ExportRefused(
                    f"{report_path} was edited by another process while this build ran "
                    f"(its bytes changed since the build started). The report is written "
                    f"only through an atomic replace, so an in-place change is a foreign "
                    f"edit; refusing to overwrite it rather than destroy that write. "
                    f"Re-run the build once nothing else is writing there."
                )
        # Promote FIRST, publish the report only once the outcome is known. The report is the
        # proof an operator reads INSTEAD of checking the bundle exists, so it must describe
        # what happened, never an assumed outcome: writing it before ``staging.rename`` meant a
        # promotion that then failed left a report claiming success -- a lie in the one artifact
        # offered as evidence. Ordering it after the rename costs at most a MISSING report when
        # the report write itself fails after a good promotion (recoverable: regenerate), which
        # is strictly better than a false one. The staging-shape checks above stay before,
        # because they are destination validation, not the outcome.
        staging.rename(out_dir)
        promoted = True
        _publish_report(report_tmp, report_path, report_before)
        report_written = True
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        staging_marker.unlink(missing_ok=True)
        # Roll the report back to exactly what was there, which for the ordinary first build
        # is nothing. Only when this run wrote it: an earlier failure leaves the operator's
        # own file untouched, and restoring bytes we never replaced would be a second bug.
        # The temp is removed whether or not the write reached the rename: a failure before
        # the rename leaves it behind, and it carries this run's id so it cannot be mistaken
        # for another build's.
        report_tmp.unlink(missing_ok=True)
        if report_written and not promoted:
            # The report was published but promotion did not complete -- restore exactly what
            # was there so no report claims a bundle that is not present. ``report_written``
            # without ``promoted`` cannot happen in the normal order (promote precedes the
            # report), so this covers only an out-of-order failure; it stays for safety.
            if report_before is None:
                report_path.unlink(missing_ok=True)
            else:
                _write_nofollow(report_path, report_before.decode("utf-8", errors="strict"))
        if promoted and not report_written:
            # Promotion landed and the report did not. The comment above the ordering accepts a
            # MISSING report as the cost of promoting first, because a missing one is
            # recoverable by regenerating. On a REBUILD the actual outcome is worse than that
            # and it is not what the ordering assumed: the PREVIOUS build's report is still
            # sitting there, describing a bundle this promotion has already replaced. Measured:
            # after a failed publication the file on disk was byte-identical to the first
            # build's, digest included, while the new bundle was promoted.
            #
            # Removed rather than rolled back -- but ONLY the stale previous-build report this
            # ordering is responsible for. The publish step refuses to overwrite a foreign
            # in-place edit (same-object-different-content) precisely so it is not destroyed;
            # unlinking unconditionally here would destroy that same foreign write on the way
            # out, undoing the refusal. So the delete is CONDITIONAL: remove the report only
            # while its bytes still equal ``report_before`` (the stale description this branch
            # owns). If they drifted -- a concurrent foreign edit -- or a foreign report was
            # created where there was none (``report_before is None`` but a file is now there),
            # the write belongs to someone else and is LEFT in place. A missing report is the
            # cost the ordering already accepts; destroying a foreign write is not.
            current = _read_text_nofollow(report_path)
            before_text = (
                None if report_before is None else report_before.decode("utf-8", errors="replace")
            )
            if current is not None and current == before_text:
                report_path.unlink(missing_ok=True)

        # If promotion did not complete, put the previous bundle back: a failed replacement
        # must leave the prior bundle reachable, never delete or orphan what was already there.
        # Keyed on ``promoted`` (not a re-stat of out_dir) so the contract reads directly.
        if not promoted and previous is not None and previous.exists() and not out_dir.exists():
            previous.rename(out_dir)
        raise
    staging_marker.unlink(missing_ok=True)
    if previous is not None:
        # Delete the aside bundle through the same move-verify-delete as the leftover purge,
        # not a bare ``rmtree(previous)``. This runs after the earlier ``_is_redirecting_entry``
        # check on ``previous``, and ``rmtree`` re-resolves the path string, so a swap between
        # that check and this delete would land the recursive delete on whatever the path names
        # now -- "build-owned by construction" does not hold once the path is re-resolved. The
        # aside was made by this build's own ``out_dir.rename(previous)``, so the verifier
        # confirms exactly that and a swapped-in tree is restored, never deleted.
        _purge_via_private_aside(
            previous,
            lambda moved: _refuse_unless_this_build_wrote_it(moved, "the aside path"),
        )

    # The number of skills SHIPPED, which is the number of selected ids -- not the number
    # of top-level entries under skills/. A skill id comes from
    # ``relative_to(skills_root).as_posix()`` and may nest, so "aws/ec2" and "aws/s3" are
    # two skills sharing one top-level "aws" directory; counting directories reported 1
    # for that pair, in the human output and in SMC_BUNDLE_JSON alike. ``included_skills``
    # is the set the plan selected and ``_copy_skill`` was driven from, so it is the same
    # population the bundle now contains.
    skill_count = len(included_skills)
    return BuildReport(
        bundle_dir=out_dir,
        digest=digest,
        skill_count=skill_count,
        mcp_servers=sorted(result.mcp),
        denied=_denied_list(candidates, plan),
        notes=result.notes,
    )


# ===========================================================================
# CLI
# ===========================================================================
def _decision_set(candidates: dict[str, list[Candidate]], plan: Plan | None) -> dict:
    included = {kind: sorted(plan.included(kind)) if plan else [] for kind in _KINDS}
    return {"included": included, "denied": _denied_list(candidates, plan)}


def _print_decision(decision: dict) -> None:
    for kind in _KINDS:
        ids = decision["included"][kind]
        print(f"  include {kind:<7} {len(ids)}: {', '.join(ids) or '(none)'}")
    print(f"  denied {len(decision['denied'])}:")
    for d in decision["denied"]:
        print(f"    - {d['kind']}/{d['id']}: {d['reason']}")


def _cmd_plan(crew_name: str, out: Path, allow: list[Path], source: Path | None) -> int:
    _refuse_unc_out(out)
    crew = resolve_crew(crew_name, source)
    agent_spec = read_agent_spec(crew)
    candidates = enumerate_all(crew, agent_spec)

    plan_path = out / PLAN_FILENAME
    if not plan_path.is_file():
        write_plan(plan_path, crew.name, candidates)
        print(f"wrote deny-by-default review template: {plan_path}")
        print("Everything is excluded. Nothing ships until you sign it and pass it with --allow.")
    else:
        print(f"review template already present: {plan_path} (left as-is)")

    plan = merge_plans(allow, crew.name)
    if plan is not None:
        verify(plan, crew.name, candidates)  # refuse an unsigned/laundered --allow early
    print("decision set (no bundle written):")
    _print_decision(_decision_set(candidates, plan))
    return 0


def _cmd_build(crew_name: str, out: Path, allow: list[Path], source: Path | None) -> int:
    _refuse_unc_out(out)
    crew = resolve_crew(crew_name, source)
    agent_spec = read_agent_spec(crew)
    candidates = enumerate_all(crew, agent_spec)

    plan = merge_plans(allow, crew.name)
    if plan is not None:
        drift = verify(plan, crew.name, candidates)
    else:
        drift = Drift()

    # The report path is validated BEFORE build_bundle, not after it.
    #
    # The check itself landed last round, at the write -- which is after build_bundle has
    # staged, moved the previous bundle aside, renamed staging into place and deleted the
    # aside copy. So it refused a foreign report only once every destructive step had already
    # run: the operator's file was intact and their bundle directory had been replaced anyway.
    # A preflight that runs after the thing it guards is a message, not a guard.
    #
    # Derived here rather than passed down, because it is derived from --out the same way the
    # writer derives it, and two spellings of one derivation is how the staging marker and
    # this path came to have different rules in the first place.
    json_path = out.parent / f"{out.name}.smc-bundle.json"
    _refuse_unless_our_report(json_path, out)

    report = build_bundle(crew, agent_spec, candidates, plan, out)

    # The report itself is written by ``build_bundle``, before the swap, so a failure there
    # cannot land after the previous bundle is gone. What stays here is the ownership check
    # above (which has to run before anything is built) and the human output below.

    # Human-readable progress first; the machine marker is the LAST line.
    print(f"bundle:  {report.bundle_dir}")
    print(f"digest:  {report.digest}")
    print(f"skills:  {report.skill_count}")
    print(f"mcp:     {', '.join(report.mcp_servers) or '(none)'}")
    if report.denied:
        print(f"denied:  {len(report.denied)} (see SMC_BUNDLE_JSON)")
    if drift.describe():
        print(f"note:    since the plan was written, {drift.describe()}")
    for note in report.notes:
        print(f"  - {note}")
    if not report.skill_count and not report.mcp_servers:
        print("Nothing private was selected: a valid bundle with the crew's persona only.")
    print(f"SMC_BUNDLE_JSON={json_path}")
    return 0


def _source_from(args_source: str | None) -> Path | None:
    raw = args_source or os.environ.get("SMC_CREW_SOURCE")
    return Path(raw).expanduser() if raw else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m packaging.build",
        description="Curate a local crew into a deployable bundle (deny-by-default).",
    )

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--crew", required=True, help="crew name")
        p.add_argument("--out", type=Path, required=True, help="bundle output directory")
        p.add_argument(
            "--allow",
            type=Path,
            action="append",
            default=[],
            metavar="PATH",
            help="a signed curation plan whose selected skills/MCP servers may ship "
            "(repeatable). Omit for an empty-but-valid bundle.",
        )
        p.add_argument(
            "--source",
            default=None,
            help="crew home holding agents/<name>.json and skills/ (defaults to the "
            "real Kiro Crew locations; $SMC_CREW_SOURCE also honoured).",
        )

    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="print the decision set and write a review template")
    _add_common(p_plan)
    p_build = sub.add_parser("build", help="write the bundle (the default verb)")
    _add_common(p_build)

    # `build` is the default verb: if the first token is neither a subcommand nor
    # a top-level help flag, inject it. Done here rather than by putting the shared
    # required args on the top parser, which would make argparse demand them before
    # the subcommand token and reject `plan --crew ...`.
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ("plan", "build", "-h", "--help"):
        pass
    else:
        raw = ["build"] + raw

    args = parser.parse_args(raw)
    source = _source_from(args.source)
    try:
        if args.cmd == "plan":
            return _cmd_plan(args.crew, args.out, args.allow, source)
        return _cmd_build(args.crew, args.out, args.allow, source)
    except ExportRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
