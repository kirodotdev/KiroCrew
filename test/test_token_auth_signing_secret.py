"""How the dashboard's HMAC signing key is published, staged and warmed before serving.

These tests pin ``token_secret._load_or_create_secret`` when the filesystem gets in the
way: a transient publish failure never creates the key's name, a filesystem without
hard links still persists one key across racing first boots, a failed directory sync
keeps a recoverable name, the staging file sits behind the keystone fence and is never
left behind, the staging directory is locked to its owner, refused when others can
write it and retried after a transient failure, a home that cannot stage at all
persists in place, an existing key is never overwritten, and a failed write leaves
nothing short behind. The gateway warms the key and the revoked-nonce store off the
event loop before serving. A topic of the ``test_token_auth`` family:
``test_token_auth.py`` keeps the token primitives, the middleware paths and the key's
first load, and the other siblings are ``test_token_auth_session_lifecycle.py``,
``test_token_auth_spa_shell.py``, ``test_token_auth_credential_scope.py`` and
``test_token_auth_tailnet_pin.py``.
"""

from __future__ import annotations

import errno
import os
import threading

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    clear_nonces,
)


def test_signing_secret_transient_publish_failure_never_creates_the_destination(
    tmp_path, monkeypatch
) -> None:
    """A publish failure that is NOT "no hard links" must leave the key path alone.

    The in-place fallback creates the destination empty and writes afterwards, so
    it carries the truncation window this fix removes. Reaching it on a *transient*
    error — a Windows sharing violation, a security product holding the path, a
    policy refusal — would recreate exactly the 0-byte key the PR exists to
    prevent, on a filesystem that can hard-link perfectly well. Only an
    unsupported-link error may unlock that fallback; anything else degrades to an
    ephemeral secret with the destination untouched.
    """
    from windows_sim import link_sharing_violation

    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE

    # A sharing violation (EACCES / WinError 32) on EVERY publish attempt: never
    # cleared, but never evidence about hard-link support either.
    with link_sharing_violation(match=ts._SECRET_KEY_FILE, times=10**6) as state:
        secret = ts._load_or_create_secret()

    assert state["n"] >= ts._CREATE_MAX_ATTEMPTS, "the publish was not retried"
    assert len(secret) >= ts._MIN_KEY_BYTES, "no usable secret for this session"
    assert not key_file.exists(), (
        "a transient publish failure created the destination anyway — a kill in "
        "that write window persists the 0-byte key this fix removes"
    )
    # No copy of ours is left either. The staging directory itself is expected --
    # the publish creates it before staging and it is masked and fenced -- so what
    # must be empty is the directory, not the data home.
    assert [p.name for p in tmp_path.iterdir()] == [
        ts._AUTH_STORE_STAGING_LEAF
    ], "a failed publish left something other than the staging directory behind"
    assert (
        list((tmp_path / ts._AUTH_STORE_STAGING_LEAF).iterdir()) == []
    ), "a staged copy of the signing key survived a failed publish"


def test_signing_secret_persists_on_a_filesystem_without_hard_links(tmp_path, monkeypatch) -> None:
    """A volume that cannot hard-link must still get a PERSISTED key.

    The publish step is ``os.link``, which FAT/exFAT and some network mounts
    reject outright. Degrading those hosts to an ephemeral secret would trade
    one breakage for another (tokens dying on every restart), so the loader
    falls back to the historical in-place create once the publish budget is
    spent.
    """
    from windows_sim import link_unsupported

    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE

    with link_unsupported(match=ts._SECRET_KEY_FILE) as state:
        secret = ts._load_or_create_secret()

    assert state["n"] >= 1, "the publish link was never attempted"
    assert key_file.exists(), "no key was persisted on a link-less filesystem"
    on_disk = key_file.read_bytes()
    assert len(on_disk) >= ts._MIN_KEY_BYTES, "persisted a short key"
    assert secret == on_disk, "returned an ephemeral key instead of the persisted one"


def test_signing_secret_concurrent_first_init_converges_without_hard_links(
    tmp_path, monkeypatch
) -> None:
    """Concurrent first boots on a LINK-LESS home must still converge on one key.

    The publish path is ``os.link``, so a filesystem that cannot hard-link sends
    every racing gateway down the in-place fallback instead. ``O_EXCL`` lets
    exactly one of them create the key; the loser must read the winner's bytes,
    not mint its own. A loser that returns an ephemeral secret while the winner
    persists a real one leaves the pair unable to validate each other's tokens —
    the divergence the exclusive create exists to prevent, and what a single
    post-loop create attempt with no re-read reintroduces.
    """
    from windows_sim import link_unsupported

    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert not key_file.exists()

    n = 8
    barrier = threading.Barrier(n)
    results: list[bytes] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _worker() -> None:
        try:
            # flake-ok: pre-existing unbounded threading.Barrier rendezvous of the racing first-boot workers, moved verbatim; tracked for a de-flake pass
            barrier.wait()
            secret = ts._load_or_create_secret()
        except BaseException as exc:  # noqa: BLE001 - surfaced via assert below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(secret)

    with link_unsupported(match=ts._SECRET_KEY_FILE):
        threads = [threading.Thread(target=_worker) for _ in range(n)]
        for th in threads:
            th.start()
        for th in threads:
            # flake-ok: pre-existing unbounded join of the racing first-boot workers, moved verbatim; tracked for a de-flake pass
            th.join()

    assert not errors, f"workers raised: {errors!r}"
    assert len(results) == n
    on_disk = key_file.read_bytes()
    assert len(on_disk) >= ts._MIN_KEY_BYTES, "no full key was persisted"
    assert set(results) == {on_disk}, (
        "racing first inits diverged on a link-less filesystem: "
        f"{len(set(results))} distinct secrets for one persisted key"
    )


def test_signing_secret_failed_dir_sync_keeps_a_recoverable_name(tmp_path, monkeypatch) -> None:
    """A directory sync that FAILS must not be followed by dropping the second name.

    The key bytes are fsynced during staging, but the published NAME lives in the
    parent directory and is not durable until that directory is synced. Unlinking
    the staging name after a failed sync leaves a power loss with neither name
    pointing at the inode: the key becomes unreachable, the next boot mints a
    fresh one, and every outstanding cookie and link signed by the old key is
    invalid — the same class of breakage this fix exists to remove.

    So a genuine sync failure keeps the staging name as a recoverable second
    reference, and still returns the key that is on disk rather than degrading to
    an ephemeral secret (which would diverge from the persisted key). The kept
    leftover is safe precisely because it sits inside the masked, fenced staging
    directory.
    """
    import errno as _errno

    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

    def _failing_sync(path, **kwargs):  # type: ignore[no-untyped-def]
        # best_effort would swallow this; strict must let it surface here.
        assert not kwargs.get("best_effort"), (
            "the publish must sync the directory STRICTLY -- best_effort hides the "
            "very EIO that makes dropping the second name unsafe"
        )
        raise OSError(_errno.EIO, "simulated: device refused the directory sync")

    monkeypatch.setattr(ts, "fsync_dir", _failing_sync)
    secret = ts._load_or_create_secret()
    # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo inside the test body, moved verbatim; tracked for a de-flake pass
    monkeypatch.undo()

    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert key_file.exists(), "the key was not published"
    assert key_file.read_bytes() == secret, (
        "returned an ephemeral secret while a valid key sits on disk -- the "
        "divergence the exclusive publish exists to prevent"
    )

    # The recoverable second name lives in the staging directory, not beside the
    # key: the data-home root is sandbox-visible, so a leftover there would be a
    # readable copy of the signing key.
    staging = tmp_path / ts._AUTH_STORE_STAGING_LEAF
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [ts._SECRET_KEY_FILE, ts._AUTH_STORE_STAGING_LEAF]
    ), "a copy of the signing key was left loose in the data-home root"
    leftovers = list(staging.iterdir())
    assert len(leftovers) == 1, (
        "a failed directory sync must keep exactly one recoverable second name, "
        f"found {[p.name for p in leftovers]}"
    )
    kept = leftovers[0]
    assert kept.read_bytes() == secret, "the kept name does not resolve to the key"
    assert kept.stat().st_ino == key_file.stat().st_ino, (
        "the kept name is a separate inode, so it is a second COPY of the signing "
        "key rather than a second name for it"
    )
    # And it is still fenced, which is what makes keeping it acceptable. Asserted
    # where the staging directory REALLY sits: the fence resolves its targets from
    # the real crew-home prefixes, so a tmp_path candidate would prove nothing.
    from kiro_crew import security

    targets = security._home_dir_targets(
        tuple(d for d in security.sensitive_home_dirs() if d.endswith(ts._AUTH_STORE_STAGING_LEAF))
    )
    assert targets, "the fence resolved no staging directory, so nothing is protected"
    for target in sorted(targets):
        assert security.is_sensitive_path(os.path.join(target, kept.name)), (
            f"a leftover {kept.name!r} in {target} would not be fenced -- it holds a "
            "full copy of the signing key and agent tools could read it"
        )


def test_signing_secret_staging_file_is_covered_by_the_keystone_fence(
    tmp_path, monkeypatch
) -> None:
    """A leftover staging file must be fenced exactly like the key it copies.

    The staged file holds the FULL signing key until the publish link lands, and a
    kill between that link and the cleanup unlink leaves it on disk. It is
    protected by living inside ``_AUTH_STORE_STAGING_LEAF``, which
    ``security.paths`` fences as a whole DIRECTORY -- so every name inside it is
    covered, present and future.

    Two things are asserted, because either alone can be true while the key is
    exposed. First, the publish really does stage INSIDE that directory: a temp
    beside the key would sit in the data-home root, which is sandbox-visible and
    same-uid writable, and no suffix rule makes that unreachable to a spawned
    shell. Second, the fence really does cover that path -- asserted where the
    directory REALLY sits, since the fence resolves its targets from the real
    crew-home prefixes and a ``tmp_path`` candidate would prove nothing.

    The no-suffix control is the point of the third assertion: the fence must
    cover a name that ends in no keystone suffix at all. That is what
    distinguishes the directory entry now doing the work from the old
    ``_KEYSTONE_ARTIFACT_SUFFIXES`` shape rule, which reached only a direct child
    of a keystone leaf's own directory and stopped applying when the temp moved
    down one level.
    """
    from pathlib import Path

    from kiro_crew import security
    from kiro_crew.dashboard import token_secret as ts

    captured: list[str] = []
    real_link = os.link

    def _spy_link(src_path, dst_path, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(str(src_path))
        return real_link(src_path, dst_path, **kwargs)

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(os, "link", _spy_link)
    ts._load_or_create_secret()
    # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo inside the test body, moved verbatim; tracked for a de-flake pass
    monkeypatch.undo()

    assert captured, "the publish never linked, so no staging name was observed"
    staged = Path(captured[0])
    assert staged.parent == tmp_path / ts._AUTH_STORE_STAGING_LEAF, (
        f"the key was staged at {staged} instead of inside "
        f"{ts._AUTH_STORE_STAGING_LEAF!r}; a temp in the data-home root is visible "
        "in every agent sandbox and link(2)-able while the write is in flight"
    )

    targets = security._home_dir_targets(
        tuple(d for d in security.sensitive_home_dirs() if d.endswith(ts._AUTH_STORE_STAGING_LEAF))
    )
    assert targets, "the fence resolved no staging directory, so nothing is protected"
    for target in sorted(targets):
        assert security.is_sensitive_path(os.path.join(target, staged.name)), (
            f"a leftover {staged.name!r} in {target} would not be fenced -- it "
            "holds a full copy of the signing key and agent tools could read it"
        )
        # Control: coverage must not depend on the filename's suffix.
        assert security.is_sensitive_path(os.path.join(target, "leftover-with-no-suffix")), (
            f"{target} is fenced only for suffixed names, so the protection is still "
            "the old shape rule rather than the directory entry"
        )


def test_signing_secret_publish_leaves_no_staging_file_behind(tmp_path, monkeypatch) -> None:
    """The staged file is this process's private file and must not survive.

    A leftover staging file is inert (the key is published under its own name)
    but it holds a full copy of the signing secret, so after a successful publish
    the staging directory must be EMPTY -- and equally after a publish this
    process LOST to a sibling, where its candidate is dropped rather than
    installed.

    The staging DIRECTORY itself is expected to remain: it is masked, fenced and
    precreated by the sandbox materialiser, so it is a fixture of the data home
    rather than an artifact of one write. What must not remain is a file in it.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    staging = tmp_path / ts._AUTH_STORE_STAGING_LEAF

    expected_home = sorted([ts._SECRET_KEY_FILE, ts._AUTH_STORE_STAGING_LEAF])

    secret = ts._load_or_create_secret()
    assert key_file.read_bytes() == secret
    assert sorted(p.name for p in tmp_path.iterdir()) == expected_home
    assert [
        p.name for p in staging.iterdir()
    ] == [], "a staged file survived the publish; it holds a full copy of the signing key"

    # Now the losing path: a key already exists, so a second loader must read it
    # and leave nothing of its own candidate behind.
    again = ts._load_or_create_secret()
    assert again == secret, "second loader diverged from the persisted key"
    assert sorted(p.name for p in tmp_path.iterdir()) == expected_home
    assert [p.name for p in staging.iterdir()] == [], "the losing loader left its candidate staged"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes only")
def test_signing_secret_survives_a_staging_dir_mode_it_cannot_narrow(tmp_path, monkeypatch) -> None:
    """A staging directory whose mode will not narrow must not block the key.

    A group- or world-accessible staging directory is worth closing, because
    another local account can then list the publish temps' names, so the
    validator chmods it. What it must not do is REFUSE when the chmod does not
    stick, because the filesystems where it cannot stick are whole classes rather
    than broken hosts: CIFS applies its mount-wide ``dir_mode`` and disregards
    chmod, and vfat/exFAT carry no POSIX mode at all.

    The validator runs BEFORE the creation loop's retry budget, so a raise there
    escapes to the outer handler, which degrades to an ephemeral secret and
    writes nothing. On such a host no boot would ever persist a signing key, so
    every restart would invalidate every dashboard session. A read bit leaks temp
    NAMES and grants no substitution, so it is the side of the boundary that
    warns. A group- or world-WRITE bit is refused instead, and
    ``test_signing_secret_refuses_a_group_writable_staging_dir`` pins that half.

    The load-bearing assertion is therefore that the key reached disk. The
    warning is asserted too, so the weaker outcome is at least not silent.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    staging = tmp_path / ts._AUTH_STORE_STAGING_LEAF
    staging.mkdir(parents=True, exist_ok=True)
    os.chmod(staging, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the wide mode IS this test's input: 0o755 carries no group/world WRITE bit, which is the boundary the validator warns on rather than refuses. lockdown-ok: a tmp_path directory, not a published artifact.  # noqa: E501  # fmt: skip

    templates: list[str] = []
    monkeypatch.setattr(ts.logger, "warning", lambda msg, *a, **kw: templates.append(str(msg)))
    # A filesystem that rejects the call outright, as vfat and exFAT do. The
    # payload write tolerates this already: both call sites pass
    # restrict_on_error="warn" for the same reason.
    monkeypatch.setattr(os, "chmod", _refusing_chmod)

    secret = ts._load_or_create_secret()

    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert key_file.exists(), (
        "a staging directory whose mode could not be narrowed stopped the signing key "
        "from being persisted at all; on such a filesystem every restart would "
        "invalidate every dashboard session"
    )
    assert key_file.read_bytes() == secret, "the returned secret is not the persisted one"
    assert any("could not be narrowed" in t for t in templates), (
        "the un-narrowable wide mode was accepted silently; it lets other local "
        "accounts list the publish temps' names, so it must be reported"
    )


def _refusing_chmod(*_args, **_kwargs) -> None:
    """A ``chmod`` that the filesystem rejects, as vfat and exFAT do."""
    raise OSError(errno.EPERM, "filesystem does not support changing modes")


def test_signing_secret_staging_dir_is_locked_to_its_owner_on_both_platforms(
    tmp_path, monkeypatch
) -> None:
    """The lockdown must run on Windows too, where no mode test can see the ACL.

    All three POSIX checks are mode-based, so a Windows staging directory a foreign
    principal can write would pass unexamined and that principal could replace a staged
    file between the write and the publish link. The directory-shaped helper is what
    expresses owner-only on both platforms, so the assertion is that it is CALLED.
    """
    from kiro_crew.dashboard import token_secret as ts

    called: list[str] = []
    monkeypatch.setattr(
        ts.platform_compat, "restrict_dir_to_owner", lambda p: called.append(str(p))
    )

    staging = tmp_path / ts._AUTH_STORE_STAGING_LEAF
    assert ts.auth_store_staging_dir(tmp_path) == staging
    assert called == [str(staging)], (
        "the staging directory was not locked to its owner; on Windows that is the only "
        "check there is, since every mode test is POSIX-gated"
    )


def test_signing_secret_staging_dir_refuses_when_windows_lockdown_fails(
    tmp_path, monkeypatch
) -> None:
    """On Windows a lockdown that fails is a refusal, because nothing else can judge it.

    On POSIX the mode split below it can see what survived and separates substitution from
    a name leak. On Windows there is no such reading, so failing to lock is the whole
    signal and it fails closed.
    """
    from kiro_crew.dashboard import token_secret as ts

    def _refusing_lockdown(_path):
        raise OSError(errno.EPERM, "cannot write the DACL")

    monkeypatch.setattr(ts.platform_compat, "restrict_dir_to_owner", _refusing_lockdown)
    monkeypatch.setattr(ts.os, "name", "nt")

    with pytest.raises(OSError) as caught:
        ts.auth_store_staging_dir(tmp_path)
    assert caught.value.errno == errno.EPERM
    assert "locked to its owner" in str(
        caught.value
    ), "the refusal must say what could not be done, or an operator cannot act on it"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes only")
def test_signing_secret_refuses_a_group_writable_staging_dir(tmp_path, monkeypatch) -> None:
    """A group- or world-WRITABLE staging directory must be refused, not narrowed-and-warned.

    A read bit only lets another local account list the publish temps' names. A WRITE bit
    lets it replace the staged file between the payload write and the publish ``os.link``,
    which installs a signing key of its choosing as the one signing every dashboard token,
    with nothing downstream able to notice.

    So the refusal is the assertion. What it must NOT do is publish in place instead: that
    writer creates the destination empty and fills it afterwards, so it would trade a
    one-``chmod`` misconfiguration for a zero-byte signing key on a host that was publishing
    atomically. The refusal degrades to an ephemeral secret for this process and leaves the
    stored file exactly as it was -- which for a first-ever publish means no file at all, and
    for an upgraded host means yesterday's key still validates yesterday's sessions.
    ``test_signing_secret_persists_in_place_when_the_home_cannot_stage_at_all`` pins the other
    side: a home that genuinely cannot stage DOES reach the in-place writer, because there the
    alternative is no persisted key on any boot.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    staging = tmp_path / ts._AUTH_STORE_STAGING_LEAF
    staging.mkdir(parents=True, exist_ok=True)
    os.chmod(staging, 0o777)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the world-writable mode IS this test's input, and refusing it is what is under test. lockdown-ok: a tmp_path directory, not a published artifact.  # noqa: E501  # fmt: skip
    monkeypatch.setattr(os, "chmod", _refusing_chmod)

    with pytest.raises(ts.AuthStoreStagingRefused) as caught:
        ts.auth_store_staging_dir(tmp_path)
    assert caught.value.errno == errno.EPERM
    assert "WRITABLE" in str(caught.value), (
        "the refusal must say which bit it refused on, or an operator cannot tell it from "
        "the read-bit case that only warns"
    )

    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert not key_file.exists(), "the fixture already had a key, so nothing below discriminates"

    secret = ts._load_or_create_secret()

    assert not key_file.exists(), (
        "the refusal published a key in place anyway; that writer creates the destination "
        "empty and fills it afterwards, so a kill in that window leaves a zero-byte signing "
        "key -- bought against a misconfiguration one chmod fixes"
    )
    assert len(secret) >= ts._MIN_KEY_BYTES, (
        "no usable secret was returned at all; the refusal degrades to an ephemeral secret "
        "for this process, it does not fail the gateway"
    )

    stored = b"p" * ts._MIN_KEY_BYTES
    key_file.write_bytes(stored)
    assert ts._load_or_create_secret() == stored, (
        "an upgraded host's stored key was not used; the staging refusal is about publishing "
        "a NEW key, not about reading the current one"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes only")
def test_signing_secret_persists_in_place_when_the_home_cannot_stage_at_all(
    tmp_path, monkeypatch
) -> None:
    """A home that cannot stage still gets a persisted key, which is the other direction.

    A non-directory occupying the staging name is not a substitution risk, it is an obstruction
    -- and the in-place writer needs no staging directory, so refusing to use it here would mean
    no boot ever persists a key and every restart invalidates every session. This is why the
    permission refusal carries its own exception type rather than being told apart by errno at
    the call site.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    blocker = tmp_path / ts._AUTH_STORE_STAGING_LEAF
    blocker.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(OSError) as caught:
        ts.auth_store_staging_dir(tmp_path)
    assert caught.value.errno == errno.ENOTDIR
    assert not isinstance(caught.value, ts.AuthStoreStagingRefused), (
        "an unusable home was raised as a permission refusal, which would degrade the key to "
        "an ephemeral secret on every boot instead of persisting one"
    )

    secret = ts._load_or_create_secret()
    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert key_file.exists(), (
        "a home that cannot stage stopped the signing key from being persisted at all, "
        "although the in-place writer needs no staging directory"
    )
    assert key_file.read_bytes() == secret, "the returned secret is not the persisted one"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes only")
def test_signing_secret_readable_staging_dir_is_not_refused(tmp_path, monkeypatch) -> None:
    """The read-bit side of the same boundary stays a warning, so the mount class still works.

    ``dir_mode=0755`` is the CIFS/vfat default rather than a hostile setting, and it grants
    no substitution. Refusing it would fail the publish on an ordinary mount.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    staging = tmp_path / ts._AUTH_STORE_STAGING_LEAF
    staging.mkdir(parents=True, exist_ok=True)
    os.chmod(staging, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the read-only-wide mode IS this test's input. lockdown-ok: a tmp_path directory, not a published artifact.  # noqa: E501  # fmt: skip
    monkeypatch.setattr(os, "chmod", _refusing_chmod)

    assert ts.auth_store_staging_dir(tmp_path) == staging, (
        "a group-readable staging directory was refused; only a WRITE bit permits the "
        "substitution the refusal exists to stop"
    )


def test_signing_secret_a_recovered_staging_failure_does_NOT_unlock_the_in_place_create(
    tmp_path, monkeypatch
) -> None:
    """The in-place path is for a home that cannot stage, not one that faltered once.

    ``_create_key_in_place`` creates the destination EMPTY and writes afterwards, so it
    carries the truncation window the staging publish exists to remove: a termination in that
    gap leaves a short key at the real name and poisons every later boot. The surrounding
    policy admits it only where the alternative is no persisted key at all -- a filesystem
    that genuinely cannot hard-link.

    A staging lookup that fails once and then succeeds is not that filesystem. If its flag
    still speaks for the loop, any home that recovered gets the truncation window handed to
    it, so a LATER link failure that should have degraded to an ephemeral secret instead
    writes the real name.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

    real_staging = ts.auth_store_staging_dir
    staging_calls: list[int] = []

    def _fail_once(home):  # type: ignore[no-untyped-def]
        staging_calls.append(1)
        if len(staging_calls) == 1:
            raise OSError(errno.EAGAIN, "staging directory briefly unavailable")
        return real_staging(home)

    def _link_always_fails(*_a, **_k):  # type: ignore[no-untyped-def]
        # NOT one of _LINK_UNSUPPORTED_ERRNOS: EPERM and friends say the filesystem cannot
        # link at all, which unlocks the in-place path on its own merits and would hide the
        # thing under test. EBUSY is the transient class, so the only route left to the
        # in-place create is a staging flag that outlived its failure.
        raise OSError(errno.EBUSY, "link target busy")

    in_place_calls: list[int] = []
    real_in_place = ts._create_key_in_place

    def _counting_in_place(key_path):  # type: ignore[no-untyped-def]
        in_place_calls.append(1)
        return real_in_place(key_path)

    monkeypatch.setattr(ts, "auth_store_staging_dir", _fail_once)
    monkeypatch.setattr(ts, "_create_key_in_place", _counting_in_place)
    monkeypatch.setattr(ts.os, "link", _link_always_fails)

    ts._load_or_create_secret()

    assert len(staging_calls) >= 2, (
        "staging never recovered, so this test is measuring the link-less case instead of the "
        "recovered one"
    )
    assert in_place_calls == [], (
        "a staging failure that had already recovered still unlocked the in-place create, so "
        "a home that merely faltered once is handed the truncation window the staging publish "
        "exists to remove"
    )


def test_signing_secret_retries_a_transient_staging_dir_failure(tmp_path, monkeypatch) -> None:
    """A staging-directory failure must be retried, not escape the budget.

    ``auth_store_staging_dir`` is resolved INSIDE the creation loop. It can fail
    for reasons that pass on the next attempt -- a sibling gateway creating the
    same directory, a Windows sharing violation, a mount briefly unavailable --
    and the loop already absorbs exactly that class of failure for the payload
    write and for the publish link.

    Resolved outside the loop it is a single point of failure: the OSError
    escapes, reaches the function's outer handler, and that handler degrades to
    an EPHEMERAL secret having written nothing. One transient miss at boot then
    costs the host its persisted signing key, which is why the discriminating
    assertion is that the key is on disk after a first-attempt failure.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

    real = ts.auth_store_staging_dir
    calls: list[int] = []

    def _fail_once(home):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            raise OSError(errno.EAGAIN, "staging directory briefly unavailable")
        return real(home)

    monkeypatch.setattr(ts, "auth_store_staging_dir", _fail_once)

    secret = ts._load_or_create_secret()

    assert len(calls) >= 2, (
        "the staging-directory failure was never retried, so it is not inside the "
        "creation loop's retry budget"
    )
    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert key_file.exists(), (
        "a transient staging-directory failure escaped the retry budget and left no "
        "persisted key, so the gateway would sign with an ephemeral secret"
    )
    assert key_file.read_bytes() == secret, "the returned secret is not the persisted one"


def test_signing_secret_binary_write_survives_windows_text_mode(tmp_path, monkeypatch) -> None:
    """Regression: the key must be written in BINARY mode.

    On Windows ``os.open()`` defaults to TEXT mode, so the ``os.write()`` that
    persists the random key translates every ``0x0A`` ('\\n') byte to
    ``0x0D 0x0A`` ('\\r\\n'). The on-disk key then grows and does not equal
    the creator's in-memory bytes (nor a sibling's read) — silent auth
    divergence, and the root cause of the flaky Windows failures in
    ``test_signing_secret_{concurrent_first_init_converges,create_contention_retries_not_ephemeral,write_failure_cleans_up_incomplete_file}``.
    The fix opens the file with ``os.O_BINARY``. Reproduced deterministically on
    POSIX via the text-mode simulator plus a key that contains an ``0x0A`` byte.
    """
    from windows_sim import windows_text_mode_write

    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE

    # A deterministic 32-byte key that CONTAINS 0x0A (LF) at index 10, so a
    # text-mode write would demonstrably corrupt it (a purely random key might
    # by chance contain no 0x0A and hide the bug).
    fixed_key = bytes(range(ts._MIN_KEY_BYTES))
    assert b"\n" in fixed_key
    monkeypatch.setattr(os, "urandom", lambda n: fixed_key[:n])

    with windows_text_mode_write(match=ts._SECRET_KEY_FILE):
        secret = ts._load_or_create_secret()

    on_disk = key_file.read_bytes()
    # With the O_BINARY fix, no translation occurs: the persisted key is the
    # exact 32 bytes and equals what was returned. Without it, the simulator
    # would have written 33 bytes ('\r\n' for the '\n') and these would differ.
    assert len(on_disk) == ts._MIN_KEY_BYTES, "newline translation changed the key length"
    assert on_disk == fixed_key, "persisted key was corrupted by text-mode newline translation"
    assert secret == on_disk, "returned key diverged from the persisted key"


def test_signing_secret_existing_file_never_overwritten(tmp_path, monkeypatch) -> None:
    """Warming must never overwrite or truncate an
    existing key file.

    A pre-existing valid key is read verbatim across repeated warms (multiple
    gateway boots against the same home); its bytes, size, and inode are
    untouched, so restarts / sibling instances keep signing with the identical
    key.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    original = b"K" * 48  # deterministic, >= 32 bytes
    key_file.write_bytes(original)
    os.chmod(key_file, 0o600)
    stat_before = key_file.stat()

    for _ in range(5):
        got = ts._load_or_create_secret()
        assert got == original, "existing key must be returned verbatim"

    assert key_file.read_bytes() == original, "existing key file was mutated"
    stat_after = key_file.stat()
    assert stat_after.st_size == len(original), "existing key file was truncated/rewritten"
    assert stat_after.st_ino == stat_before.st_ino, "existing key file was recreated"


def test_signing_secret_write_failure_cleans_up_incomplete_file(tmp_path, monkeypatch) -> None:
    """A write failure DURING exclusive
    creation must NOT leave a poisoned short key file behind.

    A creator that writes 32 bytes and fails partway (ENOSPC, quota) must leave
    NOTHING short or empty where the key belongs. An incomplete file left on disk makes every future boot's fast-path read see < 32 bytes, the
    create then hit FileExistsError, the bounded retry budget exhausted, and the
    gateway fell back to a FRESH ephemeral key on EVERY restart (tokens die on
    each restart; concurrent gateways cannot validate one another) until a human
    deleted the file by hand.

    Two mechanisms now hold that invariant. The publish path stages the key in a
    private sibling, so a failed write never touches the destination at all; the
    in-place fallback (a filesystem with no hard links) removes the creator's OWN
    incomplete file, guarded by a device+inode identity check so a racing
    sibling's valid key is never deleted. This faults EVERY key-persist write, so
    the loader exhausts the publish budget, reaches the fallback, and has to
    clean up after itself before degrading to an ephemeral secret.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert not key_file.exists()

    real_write = os.write
    calls = {"n": 0}

    def _failing_write(fd, data):  # type: ignore[no-untyped-def]
        # Fail every KEY-PERSIST write with ENOSPC, identified by its payload
        # length -- both the staged publish and the in-place fallback write the
        # key in one 32-byte call. Narrowing by length rather than by call count
        # leaves pytest's own writes alone AND makes the fault persistent, which
        # is what drives the loader through the publish retries into the cleanup
        # path. Every other write delegates, so the second init below can
        # persist a real key.
        if len(data) == ts._MIN_KEY_BYTES:
            calls["n"] += 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", _failing_write)

    # (a) A write failure still yields a working (ephemeral) secret this run.
    secret = ts._load_or_create_secret()
    assert calls["n"] >= 1, "the faulting write path was never exercised"
    assert len(secret) >= ts._MIN_KEY_BYTES

    # (b) The creator removed its OWN incomplete file — nothing short/empty is
    #     left behind to poison future boots.
    assert not key_file.exists(), "incomplete key file was left on disk (poisoned)"

    # Restore a working write and prove the path is not poisoned: the
    # next init must create a full, durable, owner-only key and return it.
    monkeypatch.setattr(os, "write", real_write)
    secret2 = ts._load_or_create_secret()
    assert key_file.exists(), "next init failed to create a key file"
    on_disk = key_file.read_bytes()
    assert len(on_disk) >= ts._MIN_KEY_BYTES, "next init wrote a short/incomplete key"
    assert secret2 == on_disk, "next init did not return the persisted key"
    # POSIX-only: Windows locks the key down via an owner DACL, not st_mode bits
    # (see test_signing_secret_concurrent_first_init_converges in
    # test_token_auth.py / test_platform_compat).
    if os.name != "nt":
        assert (key_file.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Simulates the sibling-substitution race by os.replace()-ing the key "
        "file while THIS process still holds it open. Windows forbids replacing "
        "an open handle (WinError 32 sharing violation), so the distinct-inode "
        "substitution cannot be reproduced. The identity guard's st_dev/st_ino "
        "logic keeps full coverage on the POSIX matrix."
    ),
)
def test_signing_secret_incomplete_file_not_deleted_if_replaced(tmp_path, monkeypatch) -> None:
    """The write-failure cleanup must NOT delete a valid key that a racing
    sibling substituted at the same path (identity guard on st_dev/st_ino).

    Simulate the race deterministically: the failing write, before raising,
    replaces the just-created (still-empty) key file with a DIFFERENT inode
    holding a valid 32-byte key — exactly what a sibling gateway that won a
    subsequent create would leave. The cleanup's ``os.lstat`` identity check
    must see the mismatch and leave the sibling's key untouched.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    sibling_key = b"S" * 48  # distinct, valid (>= 32 bytes)

    real_write = os.write
    calls = {"n": 0}

    def _failing_write(fd, data):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            # Replace our empty file with a sibling's valid key at a NEW inode
            # (atomic rename over the path), then fail our own write.
            tmp = tmp_path / "sibling.key"
            tmp.write_bytes(sibling_key)
            os.replace(tmp, key_file)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", _failing_write)

    secret = ts._load_or_create_secret()
    # We failed our write and fell back to ephemeral for THIS run...
    assert len(secret) >= ts._MIN_KEY_BYTES
    # ...but the sibling's valid key at the substituted inode was NOT deleted.
    assert key_file.exists(), "identity guard wrongly deleted the sibling's key"
    assert key_file.read_bytes() == sibling_key, "sibling's key bytes were mutated"


# -- Middleware warm-up: signing secret primed at construction ----------------


def test_warm_auth_singletons_primes_both_off_loop(monkeypatch) -> None:
    """Regression: warm_auth_singletons() must prime BOTH _get_secret() and
    _get_revoked_store() — off the event loop (via asyncio.to_thread) — before
    the middleware serves requests.

    Both lazily do blocking file I/O on first use (read/create
    token_signing.key + read the nonce denylist; on Windows also the owner-only
    DACL). They are not warmed synchronously in
    the token_auth_middleware() factory, because that factory runs on the loop
    via the async start_dashboard()/start_api_server(). The async startup paths
    await this helper instead, so the first auth op hits warm singletons with
    no blocking I/O on the loop.
    """
    import asyncio

    import kiro_crew.dashboard.token_auth as _ta

    calls = {"secret": 0, "store": 0}
    real_secret = _ta._get_secret
    real_store = _ta._get_revoked_store

    def spy_secret() -> bytes:
        calls["secret"] += 1
        return real_secret()

    def spy_store():
        calls["store"] += 1
        return real_store()

    monkeypatch.setattr(_ta, "_get_secret", spy_secret)
    monkeypatch.setattr(_ta, "_get_revoked_store", spy_store)

    asyncio.run(_ta.warm_auth_singletons())

    assert calls["secret"] >= 1, "warm_auth_singletons must warm _get_secret()"
    assert calls["store"] >= 1, "warm_auth_singletons must warm _get_revoked_store()"


def test_middleware_factory_does_no_blocking_warmup() -> None:
    """Source guard: the token_auth_middleware() factory body must NOT warm the
    auth singletons SYNCHRONOUSLY (a bare `_get_secret()` / `_get_revoked_store()`
    statement) — that would run blocking key-file I/O on the event loop (the
    factory is called from async start paths). Warming lives in the async
    warm_auth_singletons() helper instead, which offloads via asyncio.to_thread.

    NB: the factory's nested request-path middleware still *references*
    `_get_revoked_store()` (e.g. `_get_revoked_store().is_revoked(...)`, and a
    `to_thread`-wrapped revoke) — those are legitimate, not a synchronous
    warm-up — so we check for the bare standalone warm-up STATEMENT, not mere
    substring presence."""
    import inspect

    from kiro_crew.dashboard.token_auth import (
        token_auth_middleware,
        warm_auth_singletons,
    )

    factory_lines = {ln.strip() for ln in inspect.getsource(token_auth_middleware).splitlines()}
    # The bare synchronous warm-up statements must be gone from the factory.
    assert "_get_secret()" not in factory_lines
    assert "_get_revoked_store()" not in factory_lines

    # The async helper must offload BOTH warm-ups to a worker thread.
    warm_src = inspect.getsource(warm_auth_singletons)
    assert "asyncio.to_thread(_get_secret)" in warm_src
    assert "asyncio.to_thread(_get_revoked_store)" in warm_src


def test_start_paths_warm_auth_singletons_off_loop() -> None:
    """Source guard: both async server startup paths must await
    warm_auth_singletons() so the blocking key-file I/O is primed off the loop
    before the middleware chain is built."""
    import inspect

    from kiro_crew.dashboard import server as _srv

    for fn in (_srv.start_dashboard, _srv.start_api_server):
        src = inspect.getsource(fn)
        assert (
            "await warm_auth_singletons()" in src
        ), f"{fn.__name__} must await warm_auth_singletons() before serving"
