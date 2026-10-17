"""The masked staging directory for gateway-owned hidden FILE leaves.

A hidden leaf FILE in the crew data-home root is masked in every agent sandbox by its
exact name, and a mask covers a PATH rather than an inode. ``atomic_write`` stages its
temp beside its target, so a plain ``atomic_write`` onto such a leaf puts the whole
payload at a name no mask covers for the length of the write, and a crash leaves it
there. Everything here exists so those writes stage inside one masked DIRECTORY instead.

Kept apart from ``dashboard.token_secret`` (which re-exports these names) so a writer
that only needs the staging directory does not import the signing-key module.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import (
    RestrictErrorPolicy,
    atomic_write,
    replace_with_retry,
)

# The logger these warnings were always emitted under, kept so an operator's log filter
# (and every test that captures it) still sees them after the move out of token_secret.
logger = logging.getLogger("kiro_crew.dashboard.token_secret")

#: Directory (a direct child of the crew data home) both gateway auth stores stage their
#: temp files in: ``dashboard.token_secret``'s ``token_signing.key`` and
#: ``refresh_tokens``' ``refresh_chains.json``. Spelled as a literal rather than imported from
#: ``kiro_crew.sandbox`` to keep this module off that import chain, exactly as
#: ``service/live_target.py`` spells ``live-target-staging``; the equality is pinned by a
#: test instead.
#:
#: Every other gateway-written hidden FILE leaf in the data-home root that is published
#: with ``atomic_write`` stages here too, through
#: :func:`publish_through_auth_store_staging`: the browser extension token and
#: the Ops Mission Control secret store and policy ceiling, and the data home's ``.env``
#: (through :func:`publish_env`).
#:
#: Staging beside either leaf is what this exists to avoid. Both are masked in an agent
#: sandbox as individual FILES, and a mask covers a PATH rather than an inode, so a temp
#: beside one of them sits in the data-home root -- visible in every sandbox, and
#: ``link(2)``-able by a same-uid agent for as long as the write takes. The temp carries
#: the FULL key or chain state, so a link taken in that window keeps reading the bytes
#: after the publish, and a crash between write and publish leaves the same unmasked file
#: on disk. The spawn-time hard-link refusal cannot close this: it runs BEFORE a spawn,
#: while this window opens during one.
#:
#: The sandbox launcher masks this directory in every namespace and precreates it, and
#: ``security.paths`` fences it, both as a whole DIRECTORY -- so every temp name inside it,
#: present and future, is covered without a second entry to remember.
_AUTH_STORE_STAGING_LEAF = "auth-store-staging"


class AuthStoreStagingRefused(OSError):
    """The staging directory is REFUSED because another account could write it.

    Raised for the two permission refusals only -- a POSIX mode that keeps a group or world
    write bit, and a Windows directory that could not be locked to its owner. A home that
    simply cannot stage (a non-directory occupying the name) raises a plain ``OSError``,
    because the two need opposite answers.

    An ``OSError`` subclass so every existing caller keeps its handler: the refresh store
    marks itself degraded on either kind and does not need to tell them apart.

    The signing key does. Its publish loop treats a plain failure as "this home cannot
    stage", which unlocks ``token_secret._create_key_in_place`` -- and that writer creates the
    destination EMPTY before writing, which is the truncation window the staged publish
    exists to remove. Buying that window is worth it where no key would be persisted
    otherwise. It is not worth it here: the refusal is a one-``chmod`` misconfiguration this
    module reports in full, and an in-place publish answers nothing about it, so the key
    degrades to an ephemeral secret with the stored file untouched rather than leaving a host
    that published atomically yesterday with a zero-byte key today.
    """


def auth_store_staging_dir(home: Path) -> Path:
    """Return the auth-store staging directory under *home*, creating it if absent.

    Shared by ``token_secret``'s signing-key publish and ``refresh_tokens``' state publish so
    the two cannot drift onto different staging policies. The directory is validated
    rather than trusted, on the same grounds ``service.live_target._publish_pointer``
    validates its own: the name sits in the data-home root, so a directory found there is
    not necessarily one this process created.

    Refuses the two shapes that defeat the mask, and repairs the one that does not:

    * not a directory (a symlink included, via ``lstat``) -- REFUSED. Following it would
      stage the full key or chain state wherever it points, outside both the mask and the
      fence, which is the exposure this directory exists to close.
    * cannot be locked to its owner ON WINDOWS -- REFUSED. There
      :func:`platform_compat.restrict_dir_to_owner` writes an inheriting owner-only DACL,
      which no POSIX mode test can express, so a directory a foreign principal can write
      would otherwise pass unexamined and that principal could replace a staged file
      between the write and the publish.
    * group- or world-WRITABLE on POSIX where the narrowing does not stick -- REFUSED.
      Another local account can replace the staged file between ``atomic_write`` and the
      publish ``os.link``, which installs a signing key it chose. Nothing downstream would
      notice: the publishing process returns its own bytes while every later boot reads the
      substituted file as authoritative. A mount that cannot express modes can still
      hard-link, so "the mode did not stick" does not imply the publish will fail.
    * group- or world-READABLE or executable on POSIX -- narrowed, and WARNED when the
      narrowing does not stick. A wider read bit lets another local account enumerate the
      temps' names, so it is worth closing, but it moves no byte and grants no
      substitution. This is the ``dir_mode=0755`` class of mount (CIFS, vfat, exFAT),
      which is a real configuration rather than a hostile one.

    Refusing drops no persisted record, because each caller reaching a refusal answers it
    rather than returning silently. Both answers are DEGRADATIONS that leave the stored file
    untouched, raised as :class:`AuthStoreStagingRefused` so a caller can tell a refusal from
    a home that merely could not be staged in. The signing key degrades to an ephemeral
    secret for this process, which costs a re-login after a restart and keeps the persisted
    key byte-for-byte as it was -- it does NOT take the in-place writer, whose empty-create
    would trade a one-``chmod`` misconfiguration for a zero-byte key on a host that was
    publishing atomically. The refresh store marks itself degraded, and its own reader fails
    closed on that mark, so a spent or revoked token is REFUSED rather than accepted -- it
    does not publish a full copy of the chain state through the state file's own
    sandbox-visible directory, which is the exposure this staging directory exists to close.

    ``EXDEV`` is not a concern for the caller that publishes with ``os.link``: this is a
    subdirectory of the key's own parent, so the staged file and its destination are
    always on one filesystem.
    """
    staging = home / _AUTH_STORE_STAGING_LEAF
    try:
        st = os.lstat(staging)
    except FileNotFoundError:
        home.mkdir(parents=True, exist_ok=True)
        # exist_ok: the sandbox materialiser precreates the same directory, and the two
        # auth stores publish concurrently.
        staging.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(staging)
    if not stat.S_ISDIR(st.st_mode):
        # NOT AuthStoreStagingRefused: this says the home cannot stage at all, not that
        # another account could substitute the staged file. The key's publish loop is meant to
        # reach its in-place writer here, because the alternative is no persisted key on any
        # boot while a working path to one exists.
        raise OSError(
            errno.ENOTDIR,
            f"{staging} is not a directory; refusing to stage a gateway auth store through it",
        )
    # Owner-only on BOTH platforms, through the directory-shaped helper. Its Windows
    # arm writes an inheriting owner-only DACL, which no mode test can express, so a
    # POSIX-only check would leave a Windows staging directory a foreign principal can
    # write -- and that principal could then replace a staged file between the write
    # and the publish. Its POSIX arm is the same owner-only narrowing the mode split
    # below measures. The helper is fail-loud, and on Windows it writes only when the
    # directory's own descriptor does not already match, so the ordinary publish pays
    # nothing.
    try:
        platform_compat.restrict_dir_to_owner(staging)
    except OSError as exc:
        if os.name == "nt":
            raise AuthStoreStagingRefused(
                errno.EPERM,
                f"{staging} could not be locked to its owner ({exc}); refusing to stage a "
                "gateway auth store through it. Another account that can write this "
                "directory can replace the staged file between the write and the publish "
                "link, which installs a signing key or refresh state it chose. Grant only "
                "this account access to it, or remove it, then restart the gateway.",
            ) from exc
        # POSIX falls through: the mode test below can SEE what survived, and it
        # separates the bit that permits substitution from the bit that only leaks
        # names. Refusing on the read bit too would fail an ordinary mount.
    else:
        st = os.lstat(staging)
    if os.name != "nt" and st.st_mode & 0o022:
        raise AuthStoreStagingRefused(
            errno.EPERM,
            f"{staging} is group- or world-WRITABLE (mode {stat.S_IMODE(st.st_mode):o}) and "
            "the mode could not be narrowed; refusing to stage a gateway auth store through "
            "it. Another local account can replace the staged file between the write and the "
            "publish link, which installs a signing key it chose as the one that signs every "
            "dashboard token. chmod 700 it or remove it, then restart the gateway.",
        )
    if os.name != "nt" and st.st_mode & 0o055:
        logger.warning(
            "gateway auth-store staging directory %s is readable beyond its owner (mode %o) "
            "and could not be narrowed; other local accounts can list the publish temps' "
            "names. The bytes stay owner-only and the directory is still masked in every "
            "agent namespace and fenced from the file tools. chmod 700 it to close the "
            "listing.",
            staging,
            stat.S_IMODE(st.st_mode),
        )
    return staging


def publish_through_auth_store_staging(path: Path, content: str | bytes) -> None:
    """Atomically replace *path* with *content*, staged inside the masked staging directory.

    For a gateway-owned FILE in ``sandbox._CREW_HIDDEN_LEAVES`` that has no staged publish
    of its own. A plain ``atomic_write(path, ...)`` stages its temp with
    ``mkstemp(dir=path.parent)``: for a leaf in the data-home root that temp sits at a name
    no mask covers, holding the full payload, so a same-uid sandboxed agent can read it or
    ``link(2)`` it during the write, and a crash leaves the same bytes behind. The signing
    key and the refresh-token store keep their own publishes through the same directory,
    because each answers a staging failure differently (an ephemeral key, a degraded store).

    Here the temp is written by ``atomic_write`` INSIDE :func:`auth_store_staging_dir`
    (validated, owner-only, masked and fenced as a whole directory, precreated before the
    first spawn), so both the write window and any crash orphan are covered at every name.
    It is then renamed onto *path*. Both steps are atomic renames on one filesystem (the
    staging directory is a child of *path*'s parent), so *path* is never partial.

    Fails closed: an unusable staging directory raises and NOTHING is written. It never
    falls back to staging beside *path*, which is the exposure this exists to close. A
    failed publish removes the staged copy, so no full copy lingers in the directory. The
    file is owner-only, and a lockdown that cannot be applied raises before any byte lands.
    """
    path = Path(path)
    _publish_staged(
        auth_store_staging_dir(path.parent),
        path,
        content,
        restrict_on_error="raise",
        fsync=False,
    )


def _publish_staged(
    staging: Path,
    path: Path,
    content: str | bytes,
    *,
    restrict_on_error: RestrictErrorPolicy,
    fsync: bool,
) -> None:
    """Write *content* into the already-validated *staging* directory, then rename it.

    The publish half of :func:`publish_through_auth_store_staging`, split out so
    :func:`publish_env` can validate the directory once and answer its refusal itself.

    *restrict_on_error* is the owner-only lockdown policy for the staged FILE only, passed
    to ``atomic_write``: ``"warn"`` publishes with a warning when that file cannot be
    locked down, and the file is inside the masked directory either way. It never relaxes
    the directory validation, which the caller has already passed.

    *fsync* syncs the staged file's data before the rename, as ``atomic_write(fsync=True)``
    does. Nothing runs after the rename: a step there that raised or was interrupted would
    report a published file as failed, and a caller that rolls back on failure (the secrets
    import deletes the vault entries the new ``.env`` references) would strand it.
    """
    staged = staging / f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    try:
        atomic_write(
            staged,
            content,
            fsync=fsync,
            restrict_to_owner=True,
            restrict_on_error=restrict_on_error,
        )
        replace_with_retry(staged, path)
    except BaseException:
        # Nothing was published: drop our candidate so a full copy of the payload does
        # not linger, even inside the masked directory.
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


def publish_env(
    path: Path,
    content: str | bytes,
    *,
    restrict_on_error: RestrictErrorPolicy = "warn",
    fsync: bool = False,
) -> None:
    """Atomically replace the data home's ``.env`` with *content*, staged under the mask.

    ``.env`` is a hidden leaf FILE holding chat-channel tokens, so it takes the same
    staged publish as every other one (:func:`publish_through_auth_store_staging`). Every
    ``.env`` writer goes through here -- the setup wizard, the dashboard credential saves,
    the Weixin login and the secrets import -- so they share one staging policy:

    * unusable staging directory: raise :class:`EnvStagingRefused` and write nothing, for
      every writer. That covers a planted file or symlink at the staging name, a
      directory another account can write, and, on Windows, a staging directory that
      cannot be locked to its owner (a host that refuses the DACL, or whose user SID
      cannot be read). Writing the temp beside ``.env`` instead would put every token at
      a name no mask covers, which is the exposure this closes, so no policy relaxes it.
      The one exception type lets each writer answer with the fix the message carries
      instead of a traceback or a bare 500.
    * owner-only lockdown failure on the staged FILE, once the directory is usable: each
      writer keeps its own answer. The default ``"warn"`` lets the wizard or a credential
      save the user already typed still finish; the temp is inside the masked owner-only
      directory either way. The secrets import passes ``"raise"`` and ``fsync=True``, as
      it always has.
    """
    path = Path(path)
    try:
        staging = auth_store_staging_dir(path.parent)
    except OSError as exc:
        # A message of its own: the directory's refusal text is written for the
        # gateway auth stores (a signing key, a restart), which says nothing true about
        # a .env save. The original stays chained for the log.
        raise EnvStagingRefused(exc.errno, _env_staging_refusal(path, exc)) from exc
    if os.name != "nt" and os.lstat(staging).st_uid != os.getuid():
        # Another account's directory (left by a run as root, say) passes the mode test
        # when its owner-only chmod is refused, and the staged write would then fail
        # with a bare PermissionError instead of this refusal and its fix.
        raise EnvStagingRefused(
            errno.EACCES,
            f"{path} was not changed: Kiro Crew writes it through {staging}, and another "
            "account owns that directory. Remove it (as the account that created it, "
            "for example with sudo), then save again.",
        )
    _publish_staged(staging, path, content, restrict_on_error=restrict_on_error, fsync=fsync)


def _env_staging_refusal(path: Path, exc: OSError) -> str:
    """The refusal text for a ``.env`` save, with the fix that works for its cause."""
    staging = path.parent / _AUTH_STORE_STAGING_LEAF
    head = f"{path} was not changed: Kiro Crew writes it through {staging}"
    if exc.errno == errno.ENOTDIR:
        return f"{head}, and something that is not a directory is at that name. Remove it, then save again."
    if not isinstance(exc, AuthStoreStagingRefused):
        return f"{head}, which could not be prepared ({exc.strerror or exc}). Fix that, then save again."
    if os.name == "nt":
        # Removing the directory does not help here: it is recreated and the same
        # owner-only lock is refused again, so the fix is the volume or the account.
        return (
            f"{head}, and Windows refused to limit that directory to this account. "
            "Saving anyway would leave the tokens readable by other accounts, so the "
            "data home must be on a volume where this account can set its own "
            "permissions (NTFS), or an administrator must grant that, then save again."
        )
    # restrict_dir_to_owner already tried chmod 700 and it did not stick: the directory
    # belongs to another account, or the mount fixes the mode (CIFS, vfat).
    return (
        f"{head}, and it stays writable by other accounts after Kiro Crew tried to make it "
        "owner-only. If another account owns it, remove it; if the data home is on a mount "
        "that does not keep permission bits, move the data home to one that does. Then "
        "save again."
    )


class EnvStagingRefused(OSError):
    """A ``.env`` save was refused because the masked staging directory is unusable.

    Raised by :func:`publish_env` before any byte is written. The message names the
    directory and the one-step fix, so a writer shows it as it stands: ``str()`` is the
    message alone, without the ``[Errno N]`` prefix ``OSError`` would add.
    """

    def __str__(self) -> str:
        return str(self.strerror)
