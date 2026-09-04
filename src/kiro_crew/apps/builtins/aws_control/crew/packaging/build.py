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
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# The frozen layout the image copies in and the container reader validates.
BUNDLE_VERSION = 1
PLAN_VERSION = 1
PLAN_FILENAME = "curation-plan.json"

#: Every top-level name ``build_bundle`` writes inside its staging directory. A
#: staging path holding anything else is refused rather than deleted -- see the
#: check in ``build_bundle``. Kept beside ``PLAN_FILENAME`` because the plan is one
#: of them (it is carried across the swap).
_STAGING_OWNED_TOP_LEVEL: frozenset[str] = frozenset(
    {"agent.json", "mcp.json", "manifest.json", "skills", PLAN_FILENAME}
)


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
    if p.is_symlink():
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
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("vendor-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
)

# Filenames that are credential stores by convention, matched before any read.
# Ported verbatim from ``crew_export/scan.py:_CREDENTIAL_NAME_RE`` (that regex is
# self-contained; only the ``is_sensitive_path`` fallback, which needs
# ``kiro_crew.security``, is dropped).
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
    return any(part in _CREDENTIAL_DIR_PARTS for part in path.parts)


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
    return leaks


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


def _staged_tree_hash(staged_dir: Path, source_dir: Path) -> str:
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
    """
    rows: list[list[str]] = []
    for p in sorted(source_dir.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(source_dir).as_posix()
        shipped = staged_dir / rel
        if shipped.is_file():
            rows.append([rel, _sha(shipped.read_bytes())])
        else:
            rows.append([rel, _sha(p.read_bytes())])
    return _sha(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _tree_hash(root: Path) -> str:
    """A content hash over every file in a directory, path-and-content, sorted.

    Any byte or any filename changing changes the hash -- the property the
    content pin needs. Modelled on ``crew_export/candidates.py``'s skill
    ``tree_hash``, widened to hash every file rather than only ``SKILL.md`` so an
    edit to any file in the skill invalidates approval.
    """
    rows: list[list[str]] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
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
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _open_nofollow_under(path: Path, root: Path) -> int:
    """Open ``path`` with every component below ``root`` refusing a symlink.

    ``O_NOFOLLOW`` on a single open only refuses a FINAL-component link. The agents
    directory is writable, so an agent can leave the leaf name alone and swap a PARENT
    for a link to ``~/.ssh`` instead: the final component is then a real file, the
    single-open check passes, and the bundle carries the target's bytes. Reproduced
    before this existed -- a parent swap read private key material straight into the
    prompt that ships inside ``agent.json``.

    So each component is opened relative to the previous descriptor with ``O_NOFOLLOW``
    set, which makes a swapped directory fail at the component that was swapped rather
    than being traversed. ``root`` itself is opened normally: it is the agents directory
    the caller already resolved, not a segment the prompt names.

    This is the same opener ``backup/sidecar.py`` uses, deliberately duplicated rather
    than imported: ``crew/runtime/**`` is Linux container image source that gateway code
    must not import, which is the same reason ``_NOFOLLOW_READ_FLAGS`` is spelled twice.
    A source-text test pins the two copies equal without importing the container tree.

    Falls back to a single ``O_NOFOLLOW`` open where ``dir_fd`` is unsupported
    (Windows). That is a real narrowing and is spelled as a branch rather than hidden.
    """
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        return os.open(str(path), os.O_RDONLY | _NOFOLLOW_READ_FLAGS)

    try:
        rel = path.relative_to(root).parts
    except ValueError:
        # Not under the root the caller vouched for. _resolve_prompt_path is supposed to
        # have guaranteed this, so reaching here means a fence moved: refuse instead of
        # silently reading a path nothing anchored.
        raise ExportRefused(
            f"prompt file {path} is not under the agents directory {root}. "
            f"The read is anchored there so a swapped parent cannot be traversed, so a "
            f"path outside it cannot be read safely and is refused."
        )

    dir_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = nxt
        return os.open(rel[-1], os.O_RDONLY | _NOFOLLOW_READ_FLAGS, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _read_text_nofollow(path: Path, root: Path | None = None) -> str | None:
    """Read text through a descriptor, refusing a final-component symlink at open.

    The prompt fences in ``_resolve_prompt_path`` all run against a PATH and then
    hand that path on to be re-opened. The agents directory is writable, so the entry
    can become a link to a credential file between the last check and the read, and
    every fence would have passed. Opening once and reading from that same descriptor
    is what makes the checks binding rather than advisory.

    Returns ``None`` for content that is not UTF-8 (the caller's existing signal), and
    raises ``ExportRefused`` for the two cases that are not about encoding: the file
    is gone, or what is there is no longer a plain file.
    """
    try:
        fd = (
            _open_nofollow_under(path, root)
            if root is not None
            else os.open(str(path), os.O_RDONLY | _NOFOLLOW_READ_FLAGS)
        )
    except FileNotFoundError:
        raise ExportRefused(
            f"prompt file {path} does not exist, so the crew's persona cannot be "
            f"bundled. Exporting as-is would deploy a crew that answers as nobody."
        ) from None
    except OSError as exc:
        # ELOOP is the interesting one: the path passed every fence as a regular file
        # and is a symlink by the time it is opened.
        raise ExportRefused(
            f"prompt file {path} could not be opened as a plain file ({exc}). It "
            f"passed the prompt fences and then changed, so what it points at now was "
            f"never reviewed."
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ExportRefused(
                f"prompt file {path} is not a regular file, so it is not a persona."
            )
        # Only when O_NONBLOCK was actually applied. On Windows neither that flag
        # nor set_blocking() works on a regular-file descriptor -- it raises
        # WinError 87 -- and there is nothing to undo there anyway. Located by
        # reading the traceback: the previous attempt at this guessed os.read was
        # to blame and changed the wrong line.
        if getattr(os, "O_NONBLOCK", 0) and _NOFOLLOW_READ_FLAGS & os.O_NONBLOCK:
            os.set_blocking(fd, True)
        # Read through a file object rather than a raw os.read loop. A 1 MiB os.read
        # on Windows raises WinError 87 (invalid parameter), which reddened this on
        # the Windows shard; fdopen sizes its own buffers per platform. The
        # descriptor has already passed O_NOFOLLOW and the regular-file check, and
        # wrapping it changes neither -- closefd=False keeps the close in the
        # caller's finally, so there is exactly one close.
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read()
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def skill_candidates(skills_root: Path) -> list[Candidate]:
    """Skill directories (each dir holding a ``SKILL.md``), deny-by-default.

    Skills are global on the owner's machine and many drive ``gh``, an AWS
    profile, Playwright or the loopback gateway -- none of which exist in a
    customer-facing container -- so selection is a deployment judgement and every
    skill starts excluded.
    """
    if not skills_root.is_dir():
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
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        rel = skill_dir.relative_to(skills_root).as_posix()
        # Credential store inside the skill => blocked, never includable. Both
        # halves apply, mirroring _copy_skill and _resolve_prompt_path: a file
        # NAMED like a credential (refused_by_name) and a file LOCATED inside a
        # credential directory (refused_by_location, e.g. a nested .aws/config
        # whose basename is innocent). Catching the location half here reports
        # the skill as blocked in the curation plan rather than letting it look
        # selectable and only failing at copy time.
        cred_file = next(
            (
                p
                for p in sorted(skill_dir.rglob("*"))
                if p.is_file() and (refused_by_name(p) or refused_by_location(p))
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
        for p in sorted(skill_dir.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            text = _read_text(p)
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


def resolve_crew(name: str, source: Path | None) -> ResolvedCrew:
    """Resolve a crew's agent spec and skills root.

    With ``--source`` (or ``$SMC_CREW_SOURCE``) the root holds ``agents/`` and
    ``skills/`` -- the shape a test fixture provides. Without it, the real
    locations are used: the agent spec under ``$KIRO_HOME``/``~/.kiro/agents``
    and skills under ``$KIROCREW_HOME``. Never a temp dir.
    """
    if source is not None:
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
    path = crew.agent_spec_path
    if not path.is_file():
        raise ExportRefused(
            f"no agent spec for crew {crew.name!r} at {path}. There is nothing to "
            f"deploy; check --crew / --source."
        )
    text = _read_text(path)
    if text is None:
        raise ExportRefused(f"agent spec {path} is not decodable as UTF-8")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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
    if not path.is_file():
        raise ExportRefused(f"no curation plan at {path}. Run the plan command first.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
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
            cid = str(entry["id"])
            sel[cid] = _require_plan_include(kind, cid, entry.get("include", False))
            pin[cid] = str(entry.get("sha256") or "")
        selections[kind] = sel
        pins[kind] = pin
    return Plan(
        crew=str(raw.get("crew", "")),
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
def _resolve_prompt_path(raw: str, agents_dir: Path) -> Path:
    target = raw[len("file://") :]
    path = Path(target)
    if not path.is_absolute():
        path = (agents_dir / target).resolve()
        try:
            path.relative_to(agents_dir.resolve())
        except ValueError:
            raise ExportRefused(f"prompt URI {raw!r} escapes the agents directory") from None
    # ONE resolution, and every check below runs on its result. An earlier version
    # resolved the target for the credential fences but left this pseudo-filesystem
    # loop testing the path as written, so a symlink to /proc/self/environ passed
    # all three: the link is not under /proc, and /proc is not a credential
    # location. The read then followed the link and inlined the deploy process's
    # environment into the shipped prompt, where scan_text catches only
    # credential-SHAPED text and a secret in another format survives.
    #
    # Containment under agents_dir is deliberately NOT required: an absolute
    # persona path outside that directory is a supported case with its own test.
    #
    # ``resolved`` is a DISTINCT name rather than a reassignment of ``target``.
    # The two are different things -- the URI as written versus what it points at
    # -- and collapsing them into one name is how the symlink bug above was
    # written in the first place: every check read ``target`` and it was no longer
    # obvious which of the two any given line meant. mypy rejects the reassignment
    # outright (``target`` is the ``str`` sliced off ``raw``), which is the type
    # checker naming the same problem.
    resolved = path.resolve()
    posix = resolved.as_posix()
    for root in ("/proc", "/sys", "/dev"):
        if posix == root or posix.startswith(root + "/"):
            raise ExportRefused(
                f"prompt URI {raw!r} resolves to {resolved}, inside a "
                f"pseudo-filesystem. Those files are process and kernel state, not "
                f"a persona, and one of them is this deploy process's own "
                f"environment."
            )
    # The repo's own fence, when this module can reach it. The local predicates
    # below are a deliberate self-contained subset, and three review rounds in a
    # row found one more thing that subset does not name (a kubeconfig, then a
    # symlink, then a git credential store). A denylist needing a new entry per
    # review round is the wrong shape here, so prefer the shared implementation
    # and keep the local pair as the fallback that preserves this module's ability
    # to run without kiro_crew importable.
    try:
        from kiro_crew.security import is_sensitive_path

        _shared_fence: Callable[[str], bool] | None = is_sensitive_path
    except Exception:
        _shared_fence = None
    if _shared_fence is not None and _shared_fence(posix):
        raise ExportRefused(
            f"prompt URI {raw!r} resolves to {resolved}, which this repository "
            f"treats as a sensitive path. A prompt may reference an agent persona, "
            f"not credential or key material."
        )
    if refused_by_name(resolved) or refused_by_name(path):
        raise ExportRefused(f"prompt URI {raw!r} points at a credential location")
    if refused_by_location(resolved) or refused_by_location(path):
        raise ExportRefused(
            f"prompt URI {raw!r} resolves to {resolved}, inside a credential "
            f"directory; the file is not read. Its contents cannot be trusted to "
            f"be scannable (a kubeconfig's certificate is base64 and may match no "
            f"credential pattern), so it is refused before any read rather than "
            f"read and then scanned."
        )
    return path


def _inline_prompt(spec: dict, crew_name: str, agents_dir: Path, notes: list[str]) -> None:
    """Inline a ``file://`` prompt as literal text; refuse a missing persona.

    Kiro Crew writes an installed agent's prompt as ``file://<absolute host
    path>`` (``kiro_crew/agent.py:2166``). That path does not exist in the
    container, so a naively copied spec produces a crew that answers as nobody --
    and kiro-cli tolerates an empty prompt, so the failure is silent. Refused
    here (``serving/smc/bundle.py:validate_prompt`` refuses it at startup too).
    """
    raw = spec.get("prompt")
    if raw is None or not isinstance(raw, str) or not raw.strip():
        raise ExportRefused(
            f"agent.json for {crew_name!r} has no prompt. The prompt is the crew's "
            f"persona and kiro-cli tolerates an empty one, so a crew shipped this way "
            f"answers as nobody. Inline the persona as literal text."
        )
    if not raw.strip().lower().startswith("file://"):
        leaks = scan_text(raw, "prompt")
        if leaks:
            raise ExportRefused("the crew's prompt contains a credential: " + leaks[0].render())
        return
    path = _resolve_prompt_path(raw.strip(), agents_dir)
    # Read through a descriptor opened WITHOUT following a link at ANY component, and
    # do not re-open. _resolve_prompt_path applies every fence -- pseudo-filesystem,
    # the repo's sensitive-path predicate, the credential name and location checks --
    # and then returns a PATH. Re-opening that path here made the fences advisory: the
    # agents directory is writable, so between the last check and this read the entry
    # can become a link to ~/.aws/credentials, and the bundle would carry the target's
    # bytes with every fence having passed. Same defect the sidecar's backup read had,
    # in the opposite direction (that one exfiltrates by upload, this one by shipping
    # the bytes inside the artifact).
    #
    # agents_dir is the anchor: a single O_NOFOLLOW only refuses a FINAL-component
    # link, so without it an agent leaves the leaf alone and swaps a PARENT instead.
    # Measured -- that read private key material into the prompt.
    text = _read_text_nofollow(path, agents_dir)
    if text is None or not text.strip():
        raise ExportRefused(f"prompt file {path} is empty or not UTF-8 text")
    leaks = scan_text(text, f"prompt({path.name})")
    if leaks:
        raise ExportRefused("the crew's prompt contains a credential: " + leaks[0].render())
    spec["prompt"] = text
    notes.append(f"inlined prompt from {path} ({len(text)} chars)")


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
        block = out.get(field_name)
        if isinstance(block, dict) and block:
            out.pop(field_name)
            notes.append(
                f"mcp/{name}: dropped {field_name} ({len(block)} entr(y/ies); "
                f"supplementary and can bear a credential, so re-supply via the "
                f"deployment if needed)"
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
    if isinstance(tools, list):
        kept = [str(e) for e in tools if not _is_orphan(str(e))]
        orphans = [str(e) for e in tools if _is_orphan(str(e))]
        spec["tools"] = kept
        if orphans:
            notes.append("removed tool references with no surviving server: " + ", ".join(orphans))

    # allowedTools cannot inflate past the surviving tools: a grant for a tool the
    # bundle no longer carries is dropped.
    final_tools = set(spec.get("tools") or [])
    inherited = [t for t in (spec.get("allowedTools") or []) if isinstance(t, str)]
    granted = sorted(t for t in inherited if t in final_tools)
    if sorted(inherited) != granted:
        notes.append(f"allowedTools narrowed to surviving tools ({len(granted)} kept)")
    spec["allowedTools"] = granted

    rendered = json.dumps(spec, indent=2, ensure_ascii=False)
    if scan_text(rendered, "agent.json"):
        raise ExportRefused("the agent spec contains a credential after cleaning")

    return SpecResult(spec=spec, mcp=mcp, notes=notes)


# ===========================================================================
# Bundle writer + digest. Ported from ``crew_export/bundle.py``.
# ===========================================================================
def prompt_fingerprint(crew_name: str, pre_digest: str) -> str:
    """A value only the packaged prompt can produce.

    Every other gate can pass while the wrong crew answers. The image digest proves
    the right ARTIFACT is deployed; the container's checks prove the right bundle is
    INSTALLED; the crew address proves the request named this crew. None of them
    proves the answer came from the packaged PROMPT, and the first live deployment
    failed exactly there: a stock agent answered "reply with the single word: ok"
    indistinguishably from a tuned crew, because that question has the same answer
    either way.

    So the gate asks a question whose answer cannot be guessed or reasoned to. The
    value is derived from the bundle's own content, so it is different for every
    revision and appears nowhere except inside the prompt that shipped.
    """
    h = hashlib.sha256()
    h.update(crew_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(pre_digest.encode("utf-8"))
    return h.hexdigest()[:24]


def fingerprint_challenge(crew_name: str) -> str:
    """The exact message the deploy gate sends. Includes the crew so it cannot fire
    by accident on a normal conversation."""
    return f"SMC-VERIFY-{crew_name}"


def _inject_fingerprint_challenge(agent_json: Path, crew_name: str, fingerprint: str) -> None:
    """Prepend the challenge to the agent's prompt.

    Prepended rather than appended so it is not buried under a long persona, and
    scoped to one exact message so it cannot alter any real answer.

    It deliberately does NOT tell the model to conceal the instruction. A prompt that
    instructs an agent to hide part of itself from the person talking to it is worse
    than the leak it prevents, and there is nothing here worth hiding: the value is a
    build fingerprint, useless to anyone who obtains it.

    The cost, recorded rather than buried: every deployed prompt carries these lines,
    and the gate depends on the model complying with them. A failure therefore has
    two possible causes and the gate's message must name both.
    """
    spec = json.loads(agent_json.read_text(encoding="utf-8"))
    challenge = fingerprint_challenge(crew_name)
    block = (
        "[deployment verification]\n"
        f'If a message is exactly "{challenge}", reply with exactly this and '
        f"nothing else:\n"
        f"SMC-FINGERPRINT {fingerprint}\n"
        "This is a build fingerprint used to verify which version of this crew is "
        "deployed. If someone asks about it, you may say so. For every other "
        "message, ignore this section entirely and follow the instructions below.\n"
        "\n"
    )
    spec["prompt"] = block + (spec.get("prompt") or "")
    _write_guarded(
        agent_json,
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n",
        "agent.json",
    )


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
    for path in sorted(root.rglob("*")):
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _copy_skill(skill_dir: Path, rel: str, dest_root: Path) -> None:
    dest = dest_root / rel
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file() or p.is_symlink():
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
        text = _read_text(p)
        if text is None:
            # A binary asset cannot be scanned, so it does not ship; refusing the
            # whole skill would be harsher than the risk needs.
            continue
        _write_guarded(dest / p.relative_to(skill_dir).as_posix(), text, f"skills/{rel}/{p.name}")


@dataclass
class BuildReport:
    bundle_dir: Path
    digest: str
    fingerprint: str
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


def build_bundle(
    crew: ResolvedCrew,
    agent_spec: dict,
    candidates: dict[str, list[Candidate]],
    plan: Plan | None,
    out_dir: Path,
) -> BuildReport:
    """Write the four-entry bundle for *crew*, or refuse and leave nothing behind."""
    included_mcp = plan.included("mcp") if plan else set()
    included_skills = plan.included("skills") if plan else set()

    result = build_spec(crew, agent_spec, included_mcp, crew.agent_spec_path.parent)

    staging = out_dir.parent / (out_dir.name + ".staging")
    if staging.exists():
        # Same rule as out_dir below, and it was missed here first: this path is
        # derived from --out, so an owner can already have a directory at exactly
        # this name -- their own, or one a killed build left behind. Deleting it
        # unconditionally is the destructive half of the very hole the out_dir
        # check closes, one line above where that check was added.
        #
        # A directory this build left is recognisable: everything in it is one of
        # the names the build writes. Anything else and the path is refused rather
        # than absorbed, because the alternative is a silent recursive delete.
        residue = sorted(
            p.relative_to(staging).as_posix()
            for p in staging.rglob("*")
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
    staging.mkdir(parents=True)

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
    if out_dir.exists():
        # The SAME vocabulary the staging check above uses. It was briefly written
        # out twice, which is the duplicate-spelling mistake this branch has paid for
        # more than once: two copies of one rule drift, and here the drift would be
        # one of the two recursive deletes quietly accepting a name the other
        # refuses.
        owned = _STAGING_OWNED_TOP_LEVEL
        strangers = sorted(p.name for p in out_dir.iterdir() if p.name not in owned)
        if strangers:
            shutil.rmtree(staging, ignore_errors=True)
            raise ExportRefused(
                f"--out {out_dir} holds files this build does not own "
                f"({', '.join(strangers[:5])}"
                + (f", and {len(strangers) - 5} more" if len(strangers) > 5 else "")
                + "). Building replaces the whole directory, so it would delete "
                "them. Point --out at a fresh or previous bundle directory."
            )
        # The name check above is a check on the CONTAINER, and the delete is
        # recursive. ``skills`` is an owned NAME, so a directory holding only
        # ``skills/notes.txt`` passed it and then had notes.txt deleted by the
        # rmtree. Same shape as the credential fence that tested a symlink instead
        # of its target: the thing examined was not the thing acted on.
        #
        # So verify the whole prior bundle, not its top level. A directory this
        # build produced carries a manifest whose ``digest`` covers every file in it
        # except the manifest -- and the plan is written into staging AFTER that
        # digest is taken, so re-deriving it while skipping the plan reproduces the
        # recorded value exactly when nothing else has been added, moved or edited.
        # The name rule belongs to the ``strangers`` check above; this one adds the
        # SHAPE rule, and deliberately does not restate the names. Both scans decided
        # ownership with ``p.is_file()``, which is False for an empty directory, a FIFO,
        # a socket and a link to a directory -- so a FIFO named ``manifest.json``, or one
        # sitting at ``skills/a_fifo`` where the top-level name IS owned, passed every
        # check and was then deleted by the wholesale replacement. Measured: with the
        # old predicate a FIFO and an empty directory were both invisible.
        #
        # Descendants, not just the top level: ``strangers`` uses ``iterdir()``, which a
        # FIFO one level down never reaches.
        wrong_shape = sorted(
            p.relative_to(out_dir).as_posix()
            for p in out_dir.rglob("*")
            if _is_shape_this_build_never_writes(p)
        )
        if wrong_shape:
            shutil.rmtree(staging, ignore_errors=True)
            raise ExportRefused(
                f"--out {out_dir} holds entries of a shape this build never writes "
                f"({', '.join(wrong_shape[:5])}"
                + (f", and {len(wrong_shape) - 5} more" if len(wrong_shape) > 5 else "")
                + "). Building replaces the whole directory, so it would delete them, "
                "and a link, a FIFO or a device node is not something a previous bundle "
                "left behind. Point --out at a fresh or previous bundle directory."
            )
        entries = [p for p in out_dir.rglob("*") if p.is_file()]
        non_plan = [p for p in entries if p.relative_to(out_dir).as_posix() != PLAN_FILENAME]
        manifest_path = out_dir / "manifest.json"
        if non_plan and not manifest_path.is_file():
            shutil.rmtree(staging, ignore_errors=True)
            raise ExportRefused(
                f"--out {out_dir} has bundle-shaped contents but no manifest.json, "
                "so it is not a directory this build produced and replacing it "
                "would delete files of unknown origin. Point --out at a fresh "
                "directory or at a complete previous bundle."
            )
        if non_plan:
            try:
                recorded = json.loads(manifest_path.read_text(encoding="utf-8")).get("digest")
            except (OSError, ValueError) as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExportRefused(
                    f"--out {out_dir} has a manifest.json that cannot be read "
                    f"({exc}), so the bundle it claims to describe cannot be "
                    "verified before a recursive replace."
                ) from None
            actual = bundle_digest(out_dir, also_skip=frozenset({PLAN_FILENAME}))
            if recorded != actual:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExportRefused(
                    f"--out {out_dir} does not match the bundle its manifest "
                    "describes, so it holds at least one file this build did not "
                    "write (a nested stray such as skills/notes.txt, or an edited "
                    "file). Building replaces the directory recursively and would "
                    "delete it. Point --out at a fresh directory."
                )
        plan_file = out_dir / PLAN_FILENAME
        if plan_file.is_file():
            carried_plan = plan_file.read_bytes()

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
            if not skill_dir.is_dir():
                raise ExportRefused(f"selected skill has gone: {cid}")
            _copy_skill(skill_dir, cid, skills_dst)
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
                staged = _staged_tree_hash(skills_dst / cid, skill_dir)
                if staged != reviewed:
                    raise ExportRefused(
                        f"skills/{cid} changed while the bundle was being written, so "
                        f"the copy that would ship is not the copy that was approved."
                        f"\n  reviewed: {reviewed}\n  staged:   {staged}\n"
                        f"Re-run the plan command and look again."
                    )

        # The fingerprint is derived from the content BEFORE it is injected, which is
        # what breaks the circularity: a value computed from the finished bundle
        # cannot be part of that bundle. `digest` in the manifest is still the FINAL
        # content digest, so the container's recompute check is unaffected (the
        # manifest itself is excluded from the digest).
        fingerprint = prompt_fingerprint(crew.name, bundle_digest(staging))
        _inject_fingerprint_challenge(staging / "agent.json", crew.name, fingerprint)

        digest = bundle_digest(staging)
        _write_guarded(
            staging / "manifest.json",
            json.dumps(
                {
                    "bundle_version": BUNDLE_VERSION,
                    "crew_name": crew.name,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "digest": digest,
                    "fingerprint": fingerprint,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            "manifest.json",
        )
        if carried_plan is not None:
            # Into staging, not back into out_dir after the rename: the swap must
            # stay the last thing that happens, so a failure anywhere above leaves
            # the existing directory and its plan untouched.
            (staging / PLAN_FILENAME).write_bytes(carried_plan)
        if out_dir.exists():
            shutil.rmtree(out_dir)
        staging.rename(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    skill_count = len([p for p in (out_dir / "skills").iterdir() if p.is_dir()])
    return BuildReport(
        bundle_dir=out_dir,
        digest=digest,
        fingerprint=fingerprint,
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
    crew = resolve_crew(crew_name, source)
    agent_spec = read_agent_spec(crew)
    candidates = enumerate_all(crew, agent_spec)

    plan = merge_plans(allow, crew.name)
    if plan is not None:
        drift = verify(plan, crew.name, candidates)
    else:
        drift = Drift()

    report = build_bundle(crew, agent_spec, candidates, plan, out)

    payload = {
        "crew_name": crew.name,
        "bundle_dir": str(report.bundle_dir),
        "digest": report.digest,
        "fingerprint": report.fingerprint,
        "skill_count": report.skill_count,
        "mcp_servers": report.mcp_servers,
        "denied": report.denied,
    }
    json_path = out.parent / f"{out.name}.smc-bundle.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

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
