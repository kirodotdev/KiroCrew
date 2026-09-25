"""Ship the local KiroCrew source to the instance via S3 (private-repo safe).

The public KiroCrew repo may be private, so the EC2 box can't ``git clone`` it
anonymously. Instead the launcher packages the *local* checkout into a source
tarball, uploads it to a launcher-managed S3 bucket, and the instance downloads
it with its own IAM role (no credentials on the box, no GitHub access needed).

All S3 work goes through the :mod:`cloud.aws` ``run_aws`` chokepoint. The bucket
is created once per account/region (``kirocrew-src-<account12>-<region>``) and
reused; each launch uploads to ``<tag>/kirocrew-src.tar.gz``.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

from kiro_crew import platform_compat
from kiro_crew.cloud import aws
from kiro_crew.config.loader import config_dir
from kiro_crew.sel import sel
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

SOURCE_KEY_NAME = "kirocrew-src.tar.gz"
_BUCKET_PREFIX = "kirocrew-src-"

#: Leaf, directly under the data home, that source tarballs are built in. Not a
#: name under the stdlib temp root: see :func:`_staging_dir`.
_STAGING_DIR_LEAF = "cloud-src-staging"

# Directories never shipped to the box (rebuilt there, irrelevant, or SECRET).
# The tarfile fallback (used when `git archive` is unavailable) filters by this
# set, so it MUST cover secret-bearing dirs that `.gitignore` would otherwise
# keep out of the git-archive path — notably the KiroCrew dev-mode data dir
# (contacts, lessons, minted tokens, config) that AGENTS.md places at the repo
# root.
_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".claude",
    ".testmondata",
    # Secret-bearing / local-only dirs — never ship these off-box.
    # ``.kiro`` is the Kiro-family base (kiro-cli SSO tokens/sessions AND
    # KiroCrew's own data home ~/.kiro/crew after the data-root move); excluding
    # the whole ``.kiro`` segment covers both. The legacy ~/.kirocrew stays
    # listed for not-yet-migrated trees.
    ".kiro",
    ".kirocrew",
    ".kirocrew-dev",
    ".aws",
    ".ssh",
    ".gnupg",
}
# File suffixes / names never shipped (build artifacts + credential material).
_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".pem", ".key", ".p12", ".keystore")
# .env itself plus dotted variants (.env.local, .env.production). The dot in
# the prefix keeps innocently-named files like `.environment` shippable.
_EXCLUDE_ENV_NAME = ".env"
_EXCLUDE_ENV_PREFIX = ".env."
# Well-known credential filenames that carry no telltale suffix. The tarfile
# fallback doesn't consult .gitignore (unlike git archive), so a stray
# credentials.json or SSH key at the repo root must be caught by name.
_EXCLUDE_NAMES = frozenset(
    {
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "credentials.csv",
        ".netrc",
        ".npmrc",
        ".pypirc",
    }
)


def find_repo_root() -> Optional[Path]:
    """The Kiro Crew source root, or ``None`` when this is not a checkout.

    The non-raising half of :func:`repo_root`, for callers that must *decide*
    whether source shipping is possible rather than fail when it is not — e.g. a
    dashboard launch, which has to work from a wheel/app install where there is no
    checkout to package.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "install.sh").exists() and (parent / "setup.cfg").exists():
            return parent
    return None


def repo_root() -> Path:
    """The KiroCrew source root to package (the installed package's repo).

    Walks up from this module to the directory that contains ``install.sh`` +
    ``setup.cfg`` (the repo root), so it works from an editable checkout.

    Fails closed: if no such root is found (e.g. installed as a wheel into
    site-packages), we must NOT fall back to an ancestor like the Python
    install dir — that would tar up unrelated packages and ship them to S3.
    The caller should pass an explicit ``SourceBucket``-less git-clone path or a
    real checkout in that case.
    """
    found = find_repo_root()
    if found is not None:
        return found
    raise aws.AWSError(
        "could not locate the KiroCrew source root (no install.sh + setup.cfg "
        "above this module) — source shipping needs an editable/git checkout. "
        "Run the cloud launcher from a clone, or launch without S3 source "
        "shipping (public git-clone fallback).",
        action="source:PackageLocalCheckout",
    )


def _exclude_filter(ti: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    """Shared tar member filter: drop excluded dirs + credential-shaped files."""
    parts = set(Path(ti.name).parts)
    if parts & _EXCLUDE_DIRS:  # excluded / secret-bearing directory anywhere in path
        return None
    if ti.name.endswith(_EXCLUDE_SUFFIXES):  # credential file suffixes
        return None
    base = Path(ti.name).name
    if base == _EXCLUDE_ENV_NAME or base.startswith(_EXCLUDE_ENV_PREFIX):  # .env, .env.*
        return None
    if base in _EXCLUDE_NAMES:  # well-known credential filenames (no suffix)
        return None
    return ti


def _chain_the_launcher_owns(base: Path) -> list[Path]:
    """The directories whose state this launcher is answerable for, root-first.

    ``base`` and every directory up to and including the operator's own home, when
    ``base`` sits inside it. A directory ABOVE that home belongs to the
    administrator, or in a container to a uid not mapped into this namespace -- on a
    sandboxed host even ``/`` reads as an unmapped owner. If one of those is hostile
    then every program this account runs is already substituted and a check on one
    tarball buys nothing, while refusing there would reject an ordinary container.

    A data home the operator moved OUTSIDE their own home gets the whole chain
    instead, because that premise does not cover it: a relocated home is exactly the
    shared-host case where an ancestor can belong to a local peer.

    ``base`` may still carry a link at or above the data home -- that is the
    operator's own layout and :func:`_staging_dir` follows it -- so resolving here is
    what puts the walk on the directories that really hold the tarball. Resolving
    cannot hide a component: it replaces a link with its target, and the target's own
    ancestors then enter the chain in its place.
    """
    resolved = base.resolve()
    chain = list(reversed(resolved.parents)) + [resolved]
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return chain  # cannot place the account boundary, so check everything
    if resolved == home or home in resolved.parents:
        return [node for node in chain if node == home or home in node.parents]
    return chain


def _first_replaceable(path: Path) -> Optional[tuple[Path, str]]:
    """The outermost directory the launcher owns that another account could replace into.

    Returns that directory and why it is unsafe, or ``None`` when the chain is
    sound. Includes ``path`` itself, because replacing a directory entry needs
    permission on its PARENT rather than on the entry: the data home's own state is
    what decides whether the staging leaf inside it can be renamed away, and the
    home's ancestors decide the same for the home. Which directories are in scope is
    :func:`_chain_the_launcher_owns`.

    Two things make a directory unsafe, and its mode is only one of them.

    OWNERSHIP, first: a directory's owner can replace what is inside it whatever
    the mode says, so an ancestor owned by neither this process nor root is unsafe
    at ``0755`` exactly as much as at ``0777``. Root is trusted because a root that
    wanted to substitute the tarball does not need a directory to do it.

    Then the WRITE BITS, with the sticky exemption they carry. Under the sticky bit
    an entry may be renamed only by the entry's own owner, by the DIRECTORY's
    owner, or by root -- so sticky makes a directory safe only once its owner is
    already trusted, which is what the ownership test settles first. A sticky
    ancestor whose owner were foreign would hand that owner the same swap, which is
    why the two tests are ordered and not alternatives. Others-WRITABLE is the whole
    mode test: the swap needs the write bit, and keeping the tarball unreadable is
    the leaf's own owner-only mode's job. The GROUP bit is deliberately not a
    refusal. A host with user-private groups leaves an ordinary home group-writable
    to a group holding only the operator, so refusing on that bit would reject a
    supported host outright; and telling a shared group from a private one needs the
    supplementary-group list, not a mode bit.

    Root-first, so the answer is the outermost problem rather than an inner symptom
    of it. Windows mode bits and uids carry no ACL information, so the walk stands
    down there and the ACL the lockdown applies is the guarantee instead.
    """
    if platform_compat.IS_WINDOWS:
        return None
    mine = os.geteuid()
    for node in _chain_the_launcher_owns(path):
        try:
            info = node.stat()
        except OSError:
            # A directory this process cannot stat is one it cannot clear either,
            # so it is the answer rather than something to walk past.
            return node, "cannot be read, so it cannot be cleared"
        if info.st_uid not in (mine, 0):
            return node, f"is owned by another account (uid {info.st_uid})"
        mode = stat.S_IMODE(info.st_mode)
        if mode & stat.S_IWOTH and not mode & stat.S_ISVTX:
            return node, f"is writable by any account and not sticky (mode {mode:04o})"
    return None


def _lock_to_owner(path: Path, label: str) -> None:
    """Make ``path`` owner-only and REFUSE unless it actually is.

    Two steps, because the first cannot stand alone.

    ``platform_compat.restrict_dir_to_owner`` is the owner-only lockdown for a
    directory on both platforms and is fail-loud by contract, so a mount that
    rejects the change raises here instead of warning and letting the build carry
    on over a loose directory.

    Applying a mode is still not the same as HAVING it: a filesystem with a fixed
    permission mask -- FAT, exFAT, a CIFS mount with a permissive ``dir_mode`` --
    accepts the call and keeps its own mode, reporting success. So the mode is
    read back, and any group or other bit refuses the build. Windows mode bits
    carry no ACL information, so there the lockdown's own success is the guarantee
    and the read-back stands down rather than judging a meaningless number.

    Only for a directory this module OWNS. The data home belongs to the operator
    and is verified, never rewritten: see :func:`_staging_dir`.
    """
    try:
        platform_compat.restrict_dir_to_owner(str(path))
    except OSError as exc:
        raise aws.AWSError(
            f"cannot make the {label} '{path}' owner-only -- refusing to build the source "
            "tarball where another account could replace it.",
            action="source:PackageLocalCheckout",
        ) from exc
    if platform_compat.IS_WINDOWS:
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise aws.AWSError(
            f"cannot read the mode of the {label} '{path}' -- refusing to build the source "
            "tarball without confirming it is owner-only.",
            action="source:PackageLocalCheckout",
        ) from exc
    if mode & 0o077:
        raise aws.AWSError(
            f"the {label} '{path}' is reachable by other accounts (mode {mode:04o}) -- "
            "refusing to build the source tarball there, because the AWS CLI re-opens it "
            "by name after this process closes it.",
            action="source:PackageLocalCheckout",
        )


def _staging_dir() -> Path:
    """The launcher-owned directory a source tarball is built in.

    :func:`upload_source` hands the tarball to ``s3api put-object`` as
    ``--body <path>``, so the AWS CLI re-opens it by name after this process has
    written and closed it. The stdlib temp root is the wrong place to leave a file
    across that window: ``tempfile`` resolves that root from the environment
    (``TMPDIR`` / ``TEMP`` / ``TMP``), so the staging location is whatever the
    launcher's environment says, with no guarantee it is owner-only or sticky. A
    staging root another account can write makes the closed file swappable before
    the CLI opens it, and the launch would ship a tree that was never packaged --
    the tarball is the whole source of the box's code, so a swap there is
    arbitrary code on the instance. Resolving the directory from the data home
    keeps the environment out of it, and nothing else picks names inside it.

    Checks run ROOT-FIRST, the order
    :func:`platform_compat.first_linked_ancestor` uses and for its reason: each one
    runs only after everything above it is known good. The whole chain is covered,
    because the leaf's own mode settles nothing while an outer directory can replace
    what holds it.

    A link AT OR ABOVE the data home is the operator's own layout and is followed,
    not refused. That is where this module's trust anchor sits, the same split
    :func:`atomic_write._link_trust_anchor` draws and the one
    ``docs/system-specs/modules/workflows.md`` records for the data home: a
    symlinked ``$HOME`` (``/home/u -> /local/home/u``), a dotfile-managed
    ``~/.kiro``, and a data home moved onto another disk are all supported layouts,
    and the DEFAULT data home is lexical (``Path.home() / ".kiro" / "crew"``, see
    :func:`config.paths._default_home`), so on such a host the link arrives here on
    the path every user gets. Refusing it would refuse them a launch on grounds they
    cannot satisfy without setting ``KIROCREW_HOME`` to a pre-resolved path.

    Following it costs nothing, because what makes a link above the home dangerous
    is not the link -- it is an ancestor of the REAL directory being swappable, and
    :func:`_first_replaceable` already walks the RESOLVED chain
    (:func:`_chain_the_launcher_owns` resolves ``base`` and places the account
    boundary at the resolved home). So the ownership and mode tests land on the
    directories that actually hold the tarball rather than on a lexical chain that
    may not, which is strictly more of the real hazard than the lexical reading saw.
    It also means a link at or above the home can only have been placed by the
    operator or root: planting one needs write on its parent, and a parent another
    account can write is what those tests refuse.

    Below the anchor nothing is followed. The ``cloud-src-staging`` leaf is created
    by this module and nothing else picks names inside it, so a link there was
    planted by something else and is refused -- before the ``mkdir``, which would
    otherwise build the tree under its target, and again after it, because
    ``exist_ok=True`` accepts a pre-existing link.

    The data home belongs to the operator, so it is VERIFIED and never rewritten,
    and a refusal names the command that fixes it. Only the leaf, which this module
    owns, is locked down.
    """
    base = config_dir()
    replaceable = _first_replaceable(base)
    if replaceable is not None:
        node, reason = replaceable
        # The path IS named: it is the operator's own layout, they chose it through
        # the data-home setting, and the path is what makes the message actionable.
        raise aws.AWSError(
            f"'{node}' {reason}, so what it holds can be replaced wholesale and the AWS CLI "
            "would re-open the staged name inside the replacement -- refusing to build the "
            "source tarball. Move the data home under a directory only you can write.",
            action="source:PackageLocalCheckout",
        )
    staging = base / _STAGING_DIR_LEAF
    if platform_compat.is_link_or_junction(staging):
        raise aws.AWSError(
            f"the source staging directory '{staging}' is a link, not a real directory "
            "-- refusing to build the source tarball outside the data home.",
            action="source:PackageLocalCheckout",
        )
    # No parents=True: the leaf sits directly under the data home, which
    # ``config_dir()`` resolves and creates. A missing parent is a real error
    # here, not something to paper over with a freshly minted tree.
    staging.mkdir(exist_ok=True)
    # Re-check after mkdir: exist_ok=True happily accepts a pre-existing link,
    # and resolving both sides is what catches a component swapped higher up.
    if staging.resolve() != (base.resolve() / _STAGING_DIR_LEAF) or not staging.is_dir():
        raise aws.AWSError(
            f"the source staging directory '{staging}' does not resolve to a real directory "
            "inside the data home -- refusing to build the source tarball there.",
            action="source:PackageLocalCheckout",
        )
    _lock_to_owner(staging, "source staging directory")
    return staging


def _use_git_archive(root: Path) -> Optional[Path]:
    """Try ``git archive`` (fast, respects .gitignore). Returns the tarball or None.

    The archive is then re-filtered through the same exclusion rules as the
    tarfile fallback: git archive only ships *tracked* files, but a force-added
    ``.pem``/``.env`` would otherwise ride along — keep both paths symmetric.
    """
    out = None
    try:
        out = tempfile.NamedTemporaryFile(  # noqa: SIM115 - handed to caller
            prefix="kirocrew-src-", suffix=".tar.gz", delete=False, dir=str(_staging_dir())
        )
        out.close()
        rc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(root), "archive", "--format=tar.gz", "-o", out.name, "HEAD"],
            capture_output=True,
            timeout=120,
            **UTF8_TEXT,
        )
        if rc.returncode == 0 and Path(out.name).stat().st_size > 0:
            return _refilter_archive(Path(out.name))
    except (OSError, subprocess.SubprocessError, tarfile.TarError):
        # Includes a corrupt archive from _refilter_archive — fall through to
        # the tarfile fallback rather than propagate, after cleaning up below.
        pass
    except BaseException:
        # An interrupt during the archive run is not a fallback case, so it takes
        # the same cleanup the fallback path below does and then propagates: the
        # staging dir lives under the data home, which no reboot clears, so a
        # half-written copy of the whole checkout would stay there for good.
        if out is not None:
            Path(out.name).unlink(missing_ok=True)
        raise
    # git archive failed (or the re-filter raised) — remove the temp file so it
    # doesn't leak, then signal the caller to use the tarfile fallback.
    if out is not None:
        Path(out.name).unlink(missing_ok=True)
    return None


def _refilter_archive(archive: Path) -> Path:
    """Rewrite a tarball keeping only members that pass :func:`_exclude_filter`."""
    filtered = tempfile.NamedTemporaryFile(  # noqa: SIM115 - handed to caller
        prefix="kirocrew-src-", suffix=".tar.gz", delete=False, dir=str(_staging_dir())
    )
    filtered.close()
    try:
        with tarfile.open(archive, "r:gz") as src, tarfile.open(filtered.name, "w:gz") as dst:
            for member in src:
                if _exclude_filter(member) is None:
                    logger.info("excluding %s from source tarball", member.name)
                    continue
                fh = src.extractfile(member) if member.isreg() else None
                dst.addfile(member, fh)
    except BaseException:
        # A corrupt source archive (TarError) or any failure must not leak the
        # half-written filtered temp — the caller (_use_git_archive) only cleans
        # up `archive`, not this file. Remove it, then re-raise for the caller's
        # fall-through-to-tarfile-fallback handling.
        Path(filtered.name).unlink(missing_ok=True)
        raise
    archive.unlink(missing_ok=True)
    return Path(filtered.name)


def _custom_home_rel_parts(root: Path) -> Optional[tuple]:
    """``KIROCREW_HOME``'s path parts relative to the repo root, if it's under it.

    Dev mode (AGENTS.md) allows a custom-named data dir (e.g. ``.kirocrew-dev`` or
    an arbitrary name) anywhere inside the repo. The git-archive path is protected
    by ``.gitignore``, but the tarfile fallback isn't — so exclude that dir by its
    actual (possibly nested) path, not just the hardcoded ``.kirocrew*`` entries.
    Returns e.g. ``("data", "kc-home")`` for ``root/data/kc-home``; ``None`` when
    ``KIROCREW_HOME`` is unset or resolves outside the repo (an absolute
    ``~/.kirocrew`` isn't in the tarball anyway).
    """
    raw = os.environ.get("KIROCREW_HOME")
    if not raw:
        return None
    try:
        home = Path(raw).expanduser().resolve()
        rel = home.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return rel.parts or None


def _git_tracked_files(root: Path) -> Optional[list]:
    """The repo's tracked files (respects ``.gitignore``), or None if unavailable.

    ``git ls-files`` lists exactly what is committed/tracked — so an untracked or
    gitignored secret (``secrets.yaml``, ``.envrc``, ``local_settings.py``) is
    never returned, regardless of its name. This is what lets the tarfile
    fallback match ``git archive``'s "tracked-only" guarantee rather than being a
    best-effort denylist.
    """
    try:
        rc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if rc.returncode != 0:
        return None
    names = [n for n in rc.stdout.decode("utf-8", "replace").split("\0") if n]
    return names or None


def _tracked_tree_is_dirty(root: Path) -> bool:
    """True if the repo has uncommitted changes to TRACKED files.

    ``git archive HEAD`` packages the committed tree, so if the user edited a
    tracked file without committing, ``kirocrew cloud launch`` would silently
    ship stale (last-commit) code. We detect that here so ``build_source_tarball``
    can switch to the working-tree ``git ls-files`` tar path instead. Untracked
    files don't count (they're never shipped by either path anyway); only
    modified/staged/deleted TRACKED entries do. Fails safe to ``False`` (prefer
    the fast archive path) when git can't be queried.
    """
    try:
        rc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            timeout=60,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if rc.returncode != 0:
        return False
    return bool(rc.stdout.strip())


def _tar_fallback(root: Path) -> Path:
    """Build a source tarball from the repo's TRACKED files (fail-closed).

    Uses ``git ls-files`` so untracked / gitignored files (arbitrarily-named
    secrets) are never packaged — the denylist ``_exclude_filter`` is still
    applied on top as defense-in-depth against a force-added tracked secret, and
    a custom ``KIROCREW_HOME`` under the repo is dropped. If the tracked-file
    list can't be obtained (not a git repo / git absent), we FAIL CLOSED rather
    than fall back to walking the whole tree, which could ship a gitignored
    secret with an unrecognized name.
    """
    tracked = _git_tracked_files(root)
    if tracked is None:
        raise aws.AWSError(
            "cannot safely package the source without git (needed to honor "
            ".gitignore) — run `kirocrew cloud launch` from a git checkout, or "
            "launch without S3 source shipping (public git-clone fallback).",
            action="source:PackageLocalCheckout",
        )

    home_parts = _custom_home_rel_parts(root)  # e.g. ("data", "kc-home") or None

    def _excluded(rel: str) -> bool:
        parts = Path(rel).parts
        if set(parts) & _EXCLUDE_DIRS:
            return True
        if home_parts and tuple(parts[: len(home_parts)]) == home_parts:
            return True
        base = Path(rel).name
        if rel.endswith(_EXCLUDE_SUFFIXES):
            return True
        if base == _EXCLUDE_ENV_NAME or base.startswith(_EXCLUDE_ENV_PREFIX):
            return True
        if base in _EXCLUDE_NAMES:
            return True
        return False

    out = tempfile.NamedTemporaryFile(  # noqa: SIM115
        prefix="kirocrew-src-", suffix=".tar.gz", delete=False, dir=str(_staging_dir())
    )
    out.close()
    try:
        with tarfile.open(out.name, "w:gz") as tar:
            for rel in sorted(tracked):
                if _excluded(rel):
                    continue
                abs_path = root / rel
                if not abs_path.exists():  # tracked-but-deleted-in-worktree edge case
                    continue
                # recursive=False is REQUIRED, not just an optimization: `git ls-files`
                # lists a submodule as a single gitlink entry (its directory path).
                # tar.add() on a directory recurses by default, which would package
                # EVERY file under the submodule worktree — including untracked /
                # gitignored ones (secrets), defeating the tracked-only guarantee.
                # Each real tracked file is its own ls-files entry, so we never need
                # tar to walk a directory for us; adding non-recursively packages
                # exactly the tracked paths and skips submodule contents entirely.
                if abs_path.is_dir():
                    # A gitlink/submodule dir — don't add the directory node at all
                    # (adding it recursively would leak; adding it non-recursively
                    # just stores an empty dir entry we don't need).
                    continue
                tar.add(abs_path, arcname=rel, recursive=False)
    except BaseException:
        # An unreadable tracked file or a full disk must not leave a half-written
        # tarball behind: the staging dir lives under the data home, which no
        # reboot clears, and only the caller that gets a path back knows to
        # remove it. Drop it here, then re-raise for the caller's own handling.
        Path(out.name).unlink(missing_ok=True)
        raise
    return Path(out.name)


def build_source_tarball(root: Optional[Path] = None) -> Path:
    """Package the local source tree into a gzip tarball; return its path.

    Uses ``git archive HEAD`` (fast) for a clean checkout, but if the tracked
    working tree is DIRTY (uncommitted edits to tracked files) it uses the
    ``git ls-files`` tar path instead — otherwise the launch would silently ship
    stale last-commit code. Both paths ship only tracked files.
    """
    root = root or repo_root()
    if _tracked_tree_is_dirty(root):
        logger.info("working tree has uncommitted tracked changes; packaging the working tree")
        return _tar_fallback(root)
    archive = _use_git_archive(root)
    if archive is not None:
        logger.info("packaged source via git archive: %s", archive)
        return archive
    logger.info("git archive unavailable; using tracked-file tarfile fallback")
    return _tar_fallback(root)


def _account_id(profile: str, region: str) -> str:
    ident = aws.checked_json(
        ["sts", "get-caller-identity"], profile, region, action="sts:GetCallerIdentity"
    )
    return ident.get("Account", "") if isinstance(ident, dict) else ""


def _account_from_bucket(bucket: str) -> str:
    """Extract the 12-digit account id embedded in a ``kirocrew-src-<account>-<region>`` name.

    The launcher bucket name is the SINGLE source of truth for the account we
    pin ``--expected-bucket-owner`` to: it was resolved (fail-closed) by
    ``ensure_bucket``/``bucket_name``. Deriving the pin from the bucket string
    rather than a SECOND ``sts:get-caller-identity`` avoids the failure mode where
    a transient STS "" would silently DROP the owner pin (shipping/deleting
    without owner verification). Returns "" if the bucket isn't in the expected
    shape (e.g. the ``kirocrew-src-unknown-*`` fallback), so the caller fails
    closed instead of pinning a bogus owner.
    """
    if not bucket.startswith(_BUCKET_PREFIX):
        return ""
    # <account>-<region>: the account is the first hyphen-delimited field and is
    # always exactly 12 digits (region fields are never all-digit), so splitting
    # on the first "-" after the prefix isolates it unambiguously.
    rest = bucket[len(_BUCKET_PREFIX) :]
    candidate = rest.split("-", 1)[0]
    return candidate if candidate.isdigit() and len(candidate) == 12 else ""


def bucket_name(profile: str, region: str) -> str:
    """Deterministic per-account/region launcher bucket name."""
    account = _account_id(profile, region) or "unknown"
    return f"{_BUCKET_PREFIX}{account}-{region}"


def ensure_bucket(profile: str, region: str) -> str:
    """Create (idempotently) and return the launcher source bucket name.

    ``--expected-bucket-owner`` pins every reuse to OUR account: bucket names
    are global, so a squatter who pre-created the deterministic name would
    otherwise receive our source upload. With the pin, S3 returns 403 and we
    fail closed instead of shipping code to a stranger's bucket.
    """
    account = _account_id(profile, region)
    if not account:
        # Without the account id we can't pin --expected-bucket-owner, and the
        # deterministic global name could already belong to someone else. Fail
        # closed rather than mint an unprotected `kirocrew-src-unknown-*` bucket.
        raise aws.AWSError(
            "could not resolve the AWS account id (sts:GetCallerIdentity) — "
            "check your credentials/profile and retry.",
            action="sts:GetCallerIdentity",
        )
    bucket = f"{_BUCKET_PREFIX}{account}-{region}"
    head = ["s3api", "head-bucket", "--bucket", bucket, "--expected-bucket-owner", account]
    rc, _out, _err = aws.run_aws(head, profile, region)
    if rc != 0:
        create = ["s3api", "create-bucket", "--bucket", bucket]
        if region != "us-east-1":
            create += ["--create-bucket-configuration", f"LocationConstraint={region}"]
        aws.checked(create, profile, region, action="s3:CreateBucket")
    # Enforce the public-access block on EVERY path — freshly-created AND reused.
    # A pre-existing kirocrew-src-* bucket (older KiroCrew version, or one whose
    # BPA was later disabled) could otherwise be publicly reachable while we
    # upload (possibly proprietary) source into it. put-public-access-block is
    # idempotent, so re-asserting it on an already-blocked bucket is a no-op. We
    # FAIL CLOSED if it can't be applied rather than upload to a maybe-public
    # bucket. (SSE-S3 encryption is already the S3 default for new buckets.)
    aws.checked(
        [
            "s3api",
            "put-public-access-block",
            "--bucket",
            bucket,
            "--expected-bucket-owner",
            account,
            "--public-access-block-configuration",
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
        ],
        profile,
        region,
        action="s3:PutBucketPublicAccessBlock",
    )
    return bucket


def _audit_iam_policy_change(operation: str, arn: str, outcome: str, error: str = "") -> None:
    """Emit a structured SEL audit event for a direct IAM-policy mutation.

    The instance permissions boundary is created/deleted via the ``aws`` CLI
    (``iam create-policy`` / ``iam delete-policy``), a privileged principal
    mutation that CloudTrail records but WITHOUT the in-app context (which
    workflow triggered it). Land who/what/target/outcome on the immutable SEL
    trail too (CWE-778). Best-effort: never let audit failure break the deploy.
    """
    try:
        sel().log_api_access(
            caller="_host",
            operation=operation,
            outcome=outcome,
            source="cloud.source",
            resources=f"policy={arn}",
            error=error,
        )
    except Exception:
        logger.debug("IAM-policy-change SEL audit unavailable", exc_info=True)


def ensure_instance_boundary(profile: str = "", region: str = "") -> str:
    """Create (once, idempotently) the shared instance permissions boundary; return its ARN.

    This is the follow-up that makes the instance permissions boundary a REAL
    ceiling against a leaked *launcher* credential (not just the on-box agent).
    The boundary is a SINGLE, content-fixed managed policy named
    ``kirocrew-ec2-boundary`` (see :mod:`cloud.iam`) — the launcher CODE creates
    it here rather than per-launch CloudFormation, so:

    * it is created ONCE and reused by every launch (idempotent — an existing
      one is left untouched, never re-versioned: that immutability is the whole
      point);
    * the generated launcher policy grants only ``iam:CreatePolicy`` +
      ``iam:GetPolicy`` + ``iam:GetPolicyVersion`` on this exact name (no
      ``CreatePolicyVersion`` / ``Delete*``), so a leaked launcher credential
      can't replace an existing boundary's content — ``CreatePolicy`` on the fixed
      name fails ``EntityAlreadyExists``.

    Idempotency + content verification: we ``get-policy`` first; if present we
    **fetch its default version and compare it to the expected content-fixed
    document** (``iam.boundary_policy_document(account)``) and FAIL CLOSED on any
    mismatch — an existing boundary is only reused if its content is exactly ours.
    This closes the first-write-race gap: a *permissive* boundary seeded at this
    name (by an attacker who won the create race, or a hand-created one) is
    detected and refused rather than silently reused to cap nothing. If absent we
    ``create-policy`` once from the fixed document; a concurrent-create
    ``EntityAlreadyExists`` race is re-verified the same way. All calls go through
    the :func:`aws.run_aws` chokepoint.

    Residual (see security model in ``docs/system-specs/modules/cloud.md``):
    the first ``create-policy``
    is still a first-write race for *availability* — an attacker could seed a
    boundary that then fails our content check, blocking launches (a DoS, not an
    escalation: a mismatched boundary is refused, and can never under-cap a role).
    Operators who want to eliminate even that pre-create the boundary as an admin
    (``kirocrew cloud iam-boundary``) and drop the ``iam:CreatePolicy`` grant.
    """
    from kiro_crew.cloud import iam

    account = _require_account(profile, region)
    return _ensure_boundary(
        name=iam.BOUNDARY_NAME,
        arn=iam.boundary_arn(account),
        expected=iam.boundary_policy_document(account),
        description=(
            "Kiro Crew EC2 instance permissions ceiling (SSM core + launcher source read)."
        ),
        profile=profile,
        region=region,
    )


def ensure_crew_boundary(profile: str = "", region: str = "") -> str:
    """Create (once, idempotently) the shared Fargate crew boundary; return its ARN.

    The creator T3 asked for, and the reason ``kirocrew-fargate-crew.yaml`` can
    REQUIRE a boundary instead of defaulting to none. Same create-once,
    never-re-versioned contract as the instance boundary, and the SAME fail-closed
    content check (:func:`_verify_boundary_content`) reached through the same
    :func:`_ensure_boundary` sequence -- not a second copy of it. So an existing
    policy at this name is reused only when its content is exactly ours; a
    permissive one seeded there is refused rather than trusted to cap nothing.

    The document takes no account: the crew ceiling is content-fixed
    (:func:`iam.crew_boundary_policy_document`), so one policy serves every account
    and every region. The ARN still embeds the account, which is why this resolves
    it.
    """
    from kiro_crew.cloud import iam

    account = _require_account(profile, region)
    return _ensure_boundary(
        name=iam.CREW_BOUNDARY_NAME,
        arn=iam.crew_boundary_arn(account),
        expected=iam.crew_boundary_policy_document(),
        description=("Kiro Crew Fargate crew permissions ceiling (the four SSM channel actions)."),
        profile=profile,
        region=region,
    )


def ensure_crew_exec_boundary(profile: str = "", region: str = "") -> str:
    """Create (once, idempotently) the Fargate execution-role boundary; return its ARN.

    A THIRD boundary because a boundary caps to the intersection of the identity
    policy and the ceiling. The execution role fetches the crew's secret and opens
    its log stream BEFORE the container starts, so capping it with the task role's
    four ``ssmmessages`` actions denies both and the task never launches. Sizing one
    ceiling to cover both roles would instead be the union, which hands the task
    role the secret read that keeping it off the container exists to prevent.

    Same create-once contract and the same fail-closed content check as its two
    siblings, through the same :func:`_ensure_boundary`.
    """
    from kiro_crew.cloud import iam

    account = _require_account(profile, region)
    return _ensure_boundary(
        name=iam.CREW_EXEC_BOUNDARY_NAME,
        arn=iam.crew_exec_boundary_arn(account),
        expected=iam.crew_exec_boundary_policy_document(),
        description=(
            "Kiro Crew Fargate execution-role ceiling (crew secret read, log stream, ECR pull)."
        ),
        profile=profile,
        region=region,
    )


def _require_account(profile: str, region: str) -> str:
    """The caller's account id, or refuse: every boundary ARN embeds it."""
    account = _account_id(profile, region)
    if not account:
        raise aws.AWSError(
            "could not resolve the AWS account id (sts:GetCallerIdentity) — "
            "check your credentials/profile and retry.",
            action="sts:GetCallerIdentity",
        )
    return account


def _ensure_boundary(
    *,
    name: str,
    arn: str,
    expected: dict,
    description: str,
    profile: str,
    region: str,
) -> str:
    """Create-once-and-verify, shared by every permissions boundary this launcher owns.

    ONE copy of the sequence rather than one per lane, because the ORDER here is
    the security property and not an implementation detail: an existing policy is
    verified BEFORE it is reused, a lost create race is verified on the way back,
    and only a failure that is neither of those is surfaced as an error. A second
    lane reimplementing this would be free to get that order wrong, and reusing
    first while verifying later caps nothing for the width of the window.

    IAM is a global service; ``--region`` is harmless but passed for consistency.
    """
    import json as _json

    # Already present? VERIFY its content matches our fixed document before reusing
    # it (a permissive boundary seeded at this name must NOT be trusted to cap
    # anything).
    rc, _out, _err = aws.run_aws(["iam", "get-policy", "--policy-arn", arn], profile, region)
    if rc == 0:
        _verify_boundary_content(arn, name, expected, profile, region)
        return arn

    # Not present (or GetPolicy denied — CreatePolicy will surface the real
    # error) → create it once from the content-fixed document.
    create = [
        "iam",
        "create-policy",
        "--policy-name",
        name,
        "--description",
        description,
        "--policy-document",
        _json.dumps(expected),
    ]
    rc, _out, err = aws.run_aws(create, profile, region)
    if rc == 0:
        _audit_iam_policy_change("iam.create-policy", arn, "allowed")
        return arn
    # A concurrent launch (or a prior create) may have won the race between our
    # get-policy and create-policy — the boundary now exists, but we must VERIFY
    # its content matches ours before trusting it (the racer could have seeded a
    # permissive one). Fail closed on mismatch.
    if "EntityAlreadyExists" in (err or ""):
        _verify_boundary_content(arn, name, expected, profile, region)
        return arn
    # Any other failure (AccessDenied, throttling) is real — surface it with the
    # precise missing action so the user knows what to grant.
    missing = aws.map_missing_action(err)
    hint = f" — grant `{missing}` and retry" if missing else ""
    _audit_iam_policy_change("iam.create-policy", arn, "denied", error=(err or "").strip()[:300])
    raise aws.AWSError(
        f"could not create the permissions boundary '{name}': "
        f"{(err or '').strip()[:300]}{hint}",
        action="iam:CreatePolicy",
        missing_action=missing,
        returncode=rc,
        stderr=err or "",
    )


def _verify_boundary_content(
    arn: str, name: str, expected: dict, profile: str, region: str
) -> None:
    """Fail closed unless the existing boundary's default version equals our fixed doc.

    Shared by BOTH boundaries (``kirocrew-ec2-boundary`` and
    ``kirocrew-crew-boundary``) rather than copied per lane: the fail-closed
    comparison is the control that makes a create-once boundary trustworthy, so a
    second copy of it is a second place for the check to rot. The caller supplies
    the policy *name* (for the operator-facing message) and the *expected*
    document; everything else here is lane-independent.

    An existing policy at the fixed name must ONLY be reused if its content is
    exactly *expected* — otherwise a permissive boundary seeded there (first-write
    race, or a hand-created one) would silently cap nothing while the launcher
    creates/passes roles bounded by it. Fetches the default policy version and
    compares semantically (order-insensitive via canonical JSON). Raises
    :class:`aws.AWSError` on mismatch or if the content can't be read.
    """
    ver = aws.checked_json(
        ["iam", "get-policy", "--policy-arn", arn],
        profile,
        region,
        action="iam:GetPolicy",
    )
    default_version = ""
    if isinstance(ver, dict):
        default_version = ver.get("Policy", {}).get("DefaultVersionId", "")
    if not default_version:
        raise aws.AWSError(
            f"could not determine the default version of the existing boundary "
            f"'{name}' — refusing to reuse an unverifiable ceiling.",
            action="iam:GetPolicy",
        )
    doc_resp = aws.checked_json(
        ["iam", "get-policy-version", "--policy-arn", arn, "--version-id", default_version],
        profile,
        region,
        action="iam:GetPolicyVersion",
    )
    actual = {}
    if isinstance(doc_resp, dict):
        actual = doc_resp.get("PolicyVersion", {}).get("Document", {})
    # The CLI returns the Document as a decoded JSON object (not URL-encoded) with
    # --output json. Compare canonically (sorted keys) against our fixed document.
    if _canonical(actual) != _canonical(expected):
        raise aws.AWSError(
            f"the existing permissions boundary '{name}' does NOT match "
            "the expected content-fixed document — refusing to reuse it (a boundary "
            "that caps nothing would defeat the ceiling it exists to impose). If you "
            "intentionally changed it, delete it and let the launcher recreate it, "
            "or re-run `kirocrew cloud iam-boundary`.",
            action="iam:GetPolicyVersion",
        )


def _canonical(doc: object) -> str:
    """Order-insensitive canonical JSON for semantic policy-document comparison."""
    import json as _json

    return _json.dumps(doc, sort_keys=True, separators=(",", ":"))


def delete_instance_boundary(profile: str = "", region: str = "") -> dict:
    """Delete the shared instance permissions boundary (admin/cleanup only).

    NOT part of normal teardown — the boundary is account-shared and long-lived
    (other launches' roles reference it). Exposed for a pristine-account cleanup
    and for tests. Returns ``{"removed": bool, "arn": str, "error": str}``. Never
    raises. NB: IAM refuses to delete a policy still attached as a boundary, so
    call this only after every kirocrew-ec2-* role is gone.
    """
    from kiro_crew.cloud import iam

    account = _account_id(profile, region)
    if not account:
        return {"removed": False, "arn": "", "error": "could not resolve account id"}
    arn = iam.boundary_arn(account)
    rc, _out, err = aws.run_aws(["iam", "delete-policy", "--policy-arn", arn], profile, region)
    if rc == 0 or "NoSuchEntity" in (err or ""):
        _audit_iam_policy_change("iam.delete-policy", arn, "allowed")
        return {"removed": True, "arn": arn, "error": ""}
    _audit_iam_policy_change("iam.delete-policy", arn, "denied", error=(err or "").strip()[:300])
    return {"removed": False, "arn": arn, "error": (err or "").strip()}


def upload_source(tag: str, profile: str = "", region: str = "") -> tuple[str, str]:
    """Build + upload the source tarball for ``tag``. Returns (bucket, key)."""
    bucket = ensure_bucket(profile, region)
    key = f"{tag}/{SOURCE_KEY_NAME}"
    # Pin the upload to OUR account too: ensure_bucket's head/create pins the
    # owner, but a delete+recreate race between that check and this upload could
    # otherwise land the (proprietary) source in a bucket squatted in another
    # account. --expected-bucket-owner makes S3 return 403 in that case so we
    # fail closed rather than ship code to a stranger. We use the low-level
    # `s3api put-object` (NOT `s3 cp`): only s3api accepts --expected-bucket-owner;
    # the high-level `aws s3` commands reject it as an unknown option. Derive the
    # account from the (fail-closed-resolved) bucket name — NOT a second
    # sts:get-caller-identity, whose transient "" would silently DROP the pin and
    # ship source without owner verification. ensure_bucket already fails closed
    # if the account can't resolve, so a well-formed bucket name here guarantees
    # a real account id; fail closed if it somehow isn't.
    account = _account_from_bucket(bucket)
    if not account:
        raise aws.AWSError(
            "could not derive the account id from the launcher bucket name "
            f"'{bucket}' — refusing to upload source without an "
            "--expected-bucket-owner pin (anti-squat safety).",
            action="s3:PutObject",
        )
    tarball = build_source_tarball()
    try:
        put = [
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(tarball),
            "--expected-bucket-owner",
            account,
        ]
        aws.checked(
            put,
            profile,
            region,
            action="s3:PutObject",
            timeout=300,
        )
    finally:
        try:
            tarball.unlink()
        except OSError:
            pass
    return bucket, key


def delete_source(tag: str, profile: str = "", region: str = "") -> dict:
    """Remove a launch's uploaded source object (part of teardown).

    Returns ``{"removed": bool, "uri": str, "error": str}``. ``s3api delete-object``
    succeeds silently even when the key is already gone, so a non-zero rc is a
    real failure (denied, wrong bucket) the caller should surface — teardown must
    not silently leave a private source tarball (and its storage cost) behind.
    Never raises: the stack is already deleted by the time this runs, so a
    dangling object is a warning, not a fatal error.
    """
    from kiro_crew.cloud import ec2

    # Re-assert the tag charset here: this composes an S3 URI, and the
    # tag-is-always-validated invariant shouldn't depend on the caller.
    tag = ec2.validate_tag(tag)
    bucket = bucket_name(profile, region)
    key = f"{tag}/{SOURCE_KEY_NAME}"
    uri = f"s3://{bucket}/{key}"
    # Pin the delete to OUR account for the same anti-squat reason as the upload:
    # if the deterministic bucket name were recreated in another account, an
    # unpinned delete would silently succeed against the stranger's empty bucket,
    # masking the fact that our object was never cleaned up. Use the low-level
    # `s3api delete-object` — only s3api accepts --expected-bucket-owner (the
    # high-level `aws s3 rm` rejects it as an unknown option). Derive the account
    # from the bucket name (NOT a second sts:get-caller-identity, whose transient
    # "" would silently DROP the pin). If it can't be derived (bucket_name fell
    # back to `kirocrew-src-unknown-*` because the account didn't resolve), fail
    # closed with removed=False rather than issue an unpinned delete against a
    # possibly-foreign bucket.
    account = _account_from_bucket(bucket)
    if not account:
        return {
            "removed": False,
            "uri": uri,
            "error": (
                "could not resolve the account id to pin --expected-bucket-owner; "
                f"skipped unpinned delete of {uri} (anti-squat safety)."
            ),
        }
    rm = [
        "s3api",
        "delete-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--expected-bucket-owner",
        account,
    ]
    rc, _out, err = aws.run_aws(rm, profile, region)
    return {"removed": rc == 0, "uri": uri, "error": (err or "").strip() if rc != 0 else ""}
