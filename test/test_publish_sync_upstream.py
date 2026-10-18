"""Bidirectional sync: a publication's remote copy read back into the artifact.

Covers ``pull_upstream`` (remote ahead, up to date, both sides diverged, a live-dirty
checkpoint taken before the pull, file-backed artifacts written through to their source
file, non-text and binary upstream content refused, widgets remapped or unwrapped),
``_remote_get`` writing upstream bytes verbatim under a strict cp1252 locale, the
fork-origin target, ``clone_from_remote`` (owned, auto-sync as intent rather than
permission, non-text refused, idempotent, widgets unwrapped, foreign HTML kept),
``upstream_status`` (ahead, local ahead after a wrapper bump, live dirty, file-backed),
``overwrite_upstream`` (local forced over remote, file-backed bytes, an unpublished
artifact refused), the pullable content-type allowlist, a denied push keeping its
auto-sync intent, a refresh that does not flag an upstream that is merely ahead, and a
push that snapshots live-dirty content first, and the widget wrap/unwrap round trip. The
in-test provider and the ``store`` / ``fake_client`` / ``wire`` fixtures come from
``publish_sync_helpers.py``.
"""

from __future__ import annotations

import getpass
import io
from pathlib import Path

import pytest
from publish_sync_helpers import (  # noqa: F401 -- fixtures are requested by name
    fake_client_fixture,
    store_fixture,
    wire,
)

from kiro_crew import publish_sync
from kiro_crew.artifacts import ArtifactPublication, ForkMetadata


def _remote_get(
    tmp_path,
    content: str | bytes,
    *,
    version,
    sha,
    owner="alice",
    ctype="text/plain",
    shared=None,
    shared_v2=None,
):
    """A fake get_artifact response: artifact metadata + downloaded localPath.

    ``content`` lands on disk the way a real download does: ``bytes`` verbatim (a
    binary upstream such as a PNG), ``str`` as UTF-8. The encoding is EXPLICIT on
    purpose. A bare ``write_text`` uses the locale codec, and the hosted Windows
    runners default to cp1252, where U+0089 has no mapping -- so the binary-upstream
    tests raised ``UnicodeEncodeError('charmap')`` inside this helper before the code
    under test ran, while passing on every UTF-8 host (``PYTHONUTF8=1``, macOS,
    Linux). ``test_remote_get_*_under_a_strict_cp1252_locale`` below pins this
    without depending on the host's code page.
    """
    f = tmp_path / f"remote-{version}-{sha}.txt"
    if isinstance(content, bytes):
        f.write_bytes(content)
    else:
        f.write_text(content, encoding="utf-8")
    return {
        "artifact": {
            "title": "Doc",
            "ownerAlias": owner,
            "visibility": "PRIVATE",
            "sharedWith": shared or [],
            "sharedWithV2": shared_v2 or [],
            "currentVersionNumber": version,
            "sha256": sha,
            "contentType": ctype,
            "artifactUrl": "https://x/uuid-123",
        },
        "localPath": str(f),
    }


def _track_publication(store, slug, *, sha="sha-v1", cloud_v=1):
    pub = ArtifactPublication(
        artifact_id="uuid-123",
        view_url="https://x/uuid-123",
        last_pushed_sha256=sha,
        last_synced_kirocrew_version=1,
        version_map={"1": cloud_v},
        auto_sync=True,
    )
    return store.set_publication(slug, pub)


@pytest.mark.asyncio
async def test_pull_upstream_publication_pulls_when_ahead(store, fake_client, tmp_path):
    art = store.create(name="Doc", content="old", kind="text")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    reloaded = store.get(art.slug)
    assert reloaded.content == "collab edit"
    assert reloaded.version == 2  # new local snapshot
    assert reloaded.publication.last_synced_kirocrew_version == 2
    assert reloaded.publication.version_map["2"] == 2


@pytest.mark.asyncio
async def test_pull_upstream_up_to_date(store, fake_client, tmp_path):
    art = store.create(name="Doc", content="same", kind="text")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "same", version=1, sha="sha-v1")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is False
    assert store.get(art.slug).version == 1


@pytest.mark.asyncio
async def test_pull_upstream_no_conflict_appends_when_both_diverged(store, fake_client, tmp_path):
    """A local unsynced SNAPSHOT is already in history, so pulling never refuses
    and never clobbers it — the cloud version lands as a NEW version on top, and
    pulling NEVER pushes (the local edit is not uploaded over the upstream)."""
    art = store.create(name="Doc", content="old", kind="text")  # v1
    _track_publication(store, art.slug)
    # Local advances via an explicit snapshot → v2 (already in history), last_synced stays 1.
    store.update(art.slug, content="my local edit", snapshot=True)
    fake_client.get_response = _remote_get(tmp_path, "their edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert result.get("conflict") is not True
    reloaded = store.get(art.slug)
    assert reloaded.content == "their edit"  # cloud content is now current
    assert reloaded.version == 3  # appended on top of my v2
    assert store.get(art.slug, version=2).content == "my local edit"  # preserved in history
    assert reloaded.publication.last_synced_kirocrew_version == 3
    # Pull never pushes: the local edit was NOT uploaded over the upstream.
    assert fake_client.called("upload_version") == []


@pytest.mark.asyncio
async def test_pull_upstream_checkpoints_live_dirty_before_pull(store, fake_client, tmp_path):
    """Unsaved WORKING edits (live_dirty, not yet snapshotted) are snapshotted as
    a local-only checkpoint BEFORE the pull, so they're preserved in history and
    never lost — and the checkpoint is push-suppressed (no upload)."""
    art = store.create(name="Doc", content="old", kind="text")  # v1
    _track_publication(store, art.slug)
    # Unsaved working edit: live content changes, NO snapshot → still v1, live_dirty.
    store.update(art.slug, content="unsaved working edit")
    assert store.get(art.slug).live_dirty is True
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert result["preserved_version"] == 2  # working edit checkpointed first
    reloaded = store.get(art.slug)
    assert reloaded.content == "collab edit"
    assert reloaded.version == 3  # v1 old, v2 checkpoint(working edit), v3 pulled
    assert store.get(art.slug, version=2).content == "unsaved working edit"  # preserved
    assert reloaded.publication.last_synced_kirocrew_version == 3
    # The checkpoint did NOT auto-publish the working edit (race defused).
    assert fake_client.called("upload_version") == []


def _file_backed(store, tmp_path, content="old"):
    """A file-backed artifact whose backing file exists on disk."""
    src = tmp_path / "doc.md"
    src.write_text(content)
    art = store.create(name="Doc", content=content, kind="markdown", source_path=str(src))
    return art, src


@pytest.mark.asyncio
async def test_upstream_status_reports_ahead_for_file_backed(store, fake_client, tmp_path):
    """A file-backed publication participates in drift detection — the pull
    banner is not suppressed just because the artifact has a source_path
    (the old blanket suppression made published working files
    silently push-only)."""
    art, _src = _file_backed(store, tmp_path)
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    status = await publish_sync.upstream_status(art.slug)
    assert status["tracked"] is True
    assert status["upstream_ahead"] is True


@pytest.mark.asyncio
async def test_pull_upstream_file_backed_writes_through_to_source_file(
    store, fake_client, tmp_path
):
    """Pulling into a file-backed artifact lands the content as a new local
    version AND writes it through to the backing file on disk."""
    art, src = _file_backed(store, tmp_path)
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert src.read_text(encoding="utf-8") == "collab edit"  # write-through to the working file
    reloaded = store.get(art.slug)
    assert reloaded.content == "collab edit"
    assert reloaded.version == 2
    assert reloaded.publication.last_synced_kirocrew_version == 2


@pytest.mark.asyncio
async def test_pull_upstream_file_backed_checkpoints_external_file_edit(
    store, fake_client, tmp_path
):
    """An external edit to the backing file (live_dirty) is checkpointed as a
    local-only version BEFORE the pulled content overwrites the file — the
    pre-pull file bytes are never lost, and the checkpoint is push-suppressed."""
    art, src = _file_backed(store, tmp_path)  # v1 "old"
    _track_publication(store, art.slug)
    src.write_text("my unsaved file edit")  # external editor writes the file
    assert store.get(art.slug).live_dirty is True
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert result["preserved_version"] == 2  # file edit checkpointed first
    assert store.get(art.slug, version=2).content == "my unsaved file edit"
    assert src.read_text(encoding="utf-8") == "collab edit"  # pulled content reached the file
    assert store.get(art.slug).version == 3
    # The checkpoint did NOT auto-publish the file edit (race defused).
    assert fake_client.called("upload_version") == []


@pytest.mark.asyncio
async def test_overwrite_upstream_file_backed_pushes_live_file_bytes(store, fake_client, tmp_path):
    """Overwrite ("keep mine") is push-only — it never writes locally — so a
    file-backed artifact pushes its live file bytes like any other."""
    art, src = _file_backed(store, tmp_path)
    _track_publication(store, art.slug)
    src.write_text("my file content")  # live file bytes to push
    fake_client.get_response = _remote_get(tmp_path, "their edit", version=2, sha="sha-v2")
    result = await publish_sync.overwrite_upstream(art.slug)
    assert result["overwritten"] is True
    assert fake_client.called("upload_version")  # push landed
    assert src.read_text(encoding="utf-8") == "my file content"  # local file untouched


# A real PNG signature: 0x89 is not a valid UTF-8 lead byte, and U+0089 is unmapped in
# cp1252, so this content is non-text under BOTH codecs the helper could have picked.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


@pytest.mark.asyncio
async def test_pull_upstream_rejects_non_text_content_type(store, fake_client, tmp_path):
    """A binary upstream (image/PDF/…) is refused, not read as UTF-8 mojibake."""
    art = store.create(name="Doc", content="old", kind="text")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(
        tmp_path, _PNG_BYTES, version=2, sha="sha-v2", ctype="image/png"
    )
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is False
    assert "unsupported content type" in result["reason"]
    assert store.get(art.slug).content == "old"  # untouched


def _simulate_strict_cp1252_text_default(tmp_path, monkeypatch) -> None:
    """Make an OMITTED text encoding resolve to strict cp1252, as on hosted Windows.

    ``pathlib`` resolves a missing ``encoding`` through ``io.text_encoding``, so
    patching that one seam reproduces the runners' locale codec on any host,
    including one running under ``PYTHONUTF8=1`` (where the same tests XPASS and
    hide the bug). An explicit ``encoding=`` is passed through untouched, which is
    exactly the distinction the helper must get right.
    """
    real_text_encoding = io.text_encoding

    def _cp1252_when_omitted(encoding, stacklevel=2):
        return "cp1252" if encoding is None else real_text_encoding(encoding, stacklevel)

    monkeypatch.setattr(io, "text_encoding", _cp1252_when_omitted)
    # Prove the simulation bites before relying on it: this is the exact failure
    # the Windows shards reported from the unfixed helper. A patch that stopped
    # reaching pathlib would otherwise turn the two tests below into no-ops.
    with pytest.raises(UnicodeEncodeError):
        (tmp_path / "probe.txt").write_text("\x89PNG")


def test_remote_get_writes_binary_upstream_bytes_verbatim_under_a_strict_cp1252_locale(
    tmp_path, monkeypatch
):
    """The helper must never route binary content through the locale codec."""
    _simulate_strict_cp1252_text_default(tmp_path, monkeypatch)
    res = _remote_get(tmp_path, _PNG_BYTES, version=2, sha="sha-v2", ctype="image/png")
    assert Path(res["localPath"]).read_bytes() == _PNG_BYTES


def test_remote_get_writes_text_upstream_as_utf8_under_a_strict_cp1252_locale(
    tmp_path, monkeypatch
):
    """Text content is UTF-8 on disk regardless of locale -- the codec the provider
    reads it back with -- so a non-cp1252 character neither raises nor mojibakes."""
    _simulate_strict_cp1252_text_default(tmp_path, monkeypatch)
    text = "collab edit \u2192 \u4e2d\u6587"  # U+2192 and CJK: outside cp1252
    res = _remote_get(tmp_path, text, version=2, sha="sha-v2")
    assert Path(res["localPath"]).read_bytes() == text.encode("utf-8")


@pytest.mark.asyncio
async def test_pull_upstream_rejects_binary_upstream_under_a_strict_cp1252_locale(
    store, fake_client, tmp_path, monkeypatch
):
    """The affected scenario end to end, with the runners' codec forced on: the
    refusal is decided by content type, so no locale may turn it into an error."""
    _simulate_strict_cp1252_text_default(tmp_path, monkeypatch)
    art = store.create(name="Doc", content="old", kind="text")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(
        tmp_path, _PNG_BYTES, version=2, sha="sha-v2", ctype="image/png"
    )
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is False
    assert "unsupported content type" in result["reason"]
    assert store.get(art.slug).content == "old"


@pytest.mark.asyncio
async def test_pull_upstream_widget_remaps_kind_to_html(store, fake_client, tmp_path):
    """A FOREIGN (sentinel-less) document pulled into a widget can't be unwrapped
    to an inner fragment, so it degrades to html (keeping kind=widget would
    double-wrap a full document on the next push). The pre-pull v1 snapshot still
    renders as a widget via per-version kind."""
    art = store.create(name="W", content="<b>inner</b>", kind="widget")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(
        tmp_path,
        "<!DOCTYPE html><html><body>foreign</body></html>",
        version=2,
        sha="sha-v2",
        ctype="text/html",
    )
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    reloaded = store.get(art.slug)
    assert reloaded.kind == "html"
    assert store.get(art.slug, version=1).kind == "widget"


@pytest.mark.asyncio
async def test_pull_upstream_fork_origin(store, fake_client, tmp_path):
    art = store.create(name="F", content="old", kind="html")
    store.set_fork_metadata(
        art.slug, ForkMetadata(upstream_artifact_id="uuid-123", upstream_version=1)
    )
    fake_client.get_response = _remote_get(tmp_path, "origin v2", version=2, sha="s")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert result["source"] == "origin"
    reloaded = store.get(art.slug)
    assert reloaded.content == "origin v2"
    assert reloaded.fork_metadata.upstream_version == 2


def test_source_target_origin_resolves_fork_provider(store):
    """A fork's ``origin`` pull must resolve against the provider it was forked
    from, not always DEFAULT_PROVIDER — otherwise pulling a fork of provider B
    would query provider A (wrong content, or sync failure)."""
    art = store.create(name="Fp", content="x", kind="html")
    store.set_fork_metadata(
        art.slug,
        ForkMetadata(
            upstream_artifact_id="ext-9",
            upstream_version=1,
            upstream_provider="provider-b",
        ),
    )
    reloaded = store.get(art.slug)
    provider_name, ext_id, _v, is_pub = publish_sync._source_target(reloaded, "origin")
    assert provider_name == "provider-b"
    assert ext_id == "ext-9"
    assert is_pub is False


def test_source_target_origin_legacy_fork_falls_back_to_default(store):
    """A pre-multi-provider fork record (no upstream_provider) falls back to
    DEFAULT_PROVIDER so single-provider forks keep resolving."""
    art = store.create(name="Fl", content="x", kind="html")
    store.set_fork_metadata(art.slug, ForkMetadata(upstream_artifact_id="ext-legacy"))
    reloaded = store.get(art.slug)
    provider_name, ext_id, _v, _is_pub = publish_sync._source_target(reloaded, "origin")
    assert provider_name == publish_sync.DEFAULT_PROVIDER
    assert ext_id == "ext-legacy"


@pytest.mark.asyncio
async def test_clone_from_remote_owned(store, fake_client, tmp_path):
    me = getpass.getuser()
    fake_client.get_response = _remote_get(tmp_path, "my doc", version=2, sha="s", owner=me)
    art = await publish_sync.clone_from_remote("uuid-123")
    assert art.publication is not None
    assert art.publication.auto_sync is True  # clone always turns sync intent on
    assert art.content == "my doc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner,shared,role",
    [
        ("me", [], None),  # I own it
        ("alice", ["me"], None),  # legacy shared-with membership
        ("alice", [], "EDITOR"),
        ("alice", [], "ADMIN"),
        ("alice", [], "VIEWER"),  # view-only on the remote
        ("alice", [], "COMMENTER"),  # comment-only on the remote
        ("alice", [], None),  # no individual grant (e.g. group / LDAP only)
        ("", [], None),  # unknown owner / untrusted remote data
    ],
)
async def test_clone_from_remote_auto_sync_is_intent_not_permission(
    store, fake_client, tmp_path, owner, shared, role
):
    """auto_sync is the user's INTENT and is ALWAYS on for a clone — it is never
    derived from, or limited by, the caller's role on the remote copy. Whether a
    push actually lands is the provider's call at push time (see push_version),
    so a permission read can never wrongly pre-disable a clone — including the
    case where edit rights come via a group/LDAP grant we can't resolve locally.
    A read can prove I *can* write, but never that I *can't*; only the provider's
    denial is authoritative.
    """
    me = getpass.getuser()
    owner = me if owner == "me" else owner
    shared = [me if s == "me" else s for s in shared]
    sv2 = [{"alias": me, "role": role}] if role else []
    fake_client.get_response = _remote_get(
        tmp_path, "doc", version=1, sha="s", owner=owner, shared=shared, shared_v2=sv2
    )
    art = await publish_sync.clone_from_remote("uuid-123")
    assert art.publication is not None
    assert art.publication.auto_sync is True
    assert art.publication.published_by == owner  # mirrors the remote owner


@pytest.mark.asyncio
async def test_clone_rejects_non_text_content_type(store, fake_client, tmp_path):
    """Clone refuses a binary artifact rather than reading it as UTF-8 mojibake."""
    me = getpass.getuser()
    fake_client.get_response = _remote_get(
        tmp_path, "%PDF-1.7...", version=1, sha="s", owner=me, ctype="application/pdf"
    )
    with pytest.raises(publish_sync.PublishError) as ei:
        await publish_sync.clone_from_remote("uuid-123")
    assert "not a text artifact" in str(ei.value)


@pytest.mark.asyncio
async def test_clone_idempotent(store, fake_client, tmp_path):
    me = getpass.getuser()
    fake_client.get_response = _remote_get(tmp_path, "doc", version=1, sha="s", owner=me)
    a1 = await publish_sync.clone_from_remote("uuid-123")
    a2 = await publish_sync.clone_from_remote("uuid-123")
    assert a1.slug == a2.slug


@pytest.mark.asyncio
async def test_clone_from_remote_widget_unwraps_to_widget(store, fake_client, tmp_path):
    """A widget published remotely is a sentinel-wrapped standalone document.
    Cloning it locally recovers the inner fragment and keeps kind=widget, so a
    cloned remote-only widget is still inline-embeddable in chat (not degraded
    to html) — mirroring pull_upstream's unwrap. v1's per-version kind is widget
    so the clone is convertible/revertable as a widget."""
    me = getpass.getuser()
    wrapped = publish_sync.wrap_widget_html("<b>cloned widget</b>")
    fake_client.get_response = _remote_get(
        tmp_path, wrapped, version=1, sha="s", owner=me, ctype="text/html"
    )
    art = await publish_sync.clone_from_remote("uuid-123")
    assert art.kind == "widget"  # recovered fragment → still a widget
    assert art.content == "<b>cloned widget</b>"  # exact inner, unwrapped
    assert art.version_kinds.get("1") == "widget"
    assert art.publication is not None


@pytest.mark.asyncio
async def test_clone_from_remote_foreign_html_stays_html(store, fake_client, tmp_path):
    """A foreign / sentinel-less HTML document clones as html (no false widget)."""
    me = getpass.getuser()
    fake_client.get_response = _remote_get(
        tmp_path,
        "<!DOCTYPE html><html><body>foreign</body></html>",
        version=1,
        sha="s",
        owner=me,
        ctype="text/html",
    )
    art = await publish_sync.clone_from_remote("uuid-123")
    assert art.kind == "html"


@pytest.mark.asyncio
async def test_upstream_status_reports_ahead(store, fake_client, tmp_path):
    art = store.create(name="Doc", content="x", kind="text")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "ignored", version=5, sha="s")
    status = await publish_sync.upstream_status(art.slug)
    assert status["tracked"] is True
    assert status["upstream_ahead"] is True
    assert status["cloud_version"] == 5
    assert status["source"] == "publication"
    assert status["live_dirty"] is False


@pytest.mark.asyncio
async def test_upstream_status_reports_local_ahead_on_wrapper_revision_bump(
    store, fake_client, tmp_path, monkeypatch
):
    """Widget with stale wrapper_revision shows local_ahead even if content hasn't changed."""
    art = store.create(name="W", content="<p>x</p>", kind="widget")
    _track_publication(store, art.slug)
    # Simulate wrapper bump.
    monkeypatch.setattr(publish_sync, "WRAPPER_REVISION", publish_sync.WRAPPER_REVISION + 1)
    fake_client.get_response = _remote_get(tmp_path, "ignored", version=1, sha="sha-v1")
    status = await publish_sync.upstream_status(art.slug)
    assert status["local_ahead"] is True


@pytest.mark.asyncio
async def test_upstream_status_reports_live_dirty(store, fake_client, tmp_path):
    """Unsaved working edits surface as live_dirty so the banner can tell the
    user their edits will be preserved (checkpointed) before a pull."""
    art = store.create(name="Doc", content="x", kind="text")
    _track_publication(store, art.slug)
    store.update(art.slug, content="unsaved edit")  # no snapshot → live_dirty
    fake_client.get_response = _remote_get(tmp_path, "ignored", version=5, sha="s")
    status = await publish_sync.upstream_status(art.slug)
    assert status["live_dirty"] is True
    assert status["upstream_ahead"] is True


@pytest.mark.asyncio
async def test_push_version_denial_keeps_auto_sync_intent(store, fake_client):
    """A push the provider rejects for lack of write access records a clear
    last_error but NEVER disables auto_sync. auto_sync is the user's sync INTENT,
    decoupled from permission: the provider re-evaluates write access on the NEXT
    snapshot's push, so a later grant resumes syncing automatically and a revoke
    just keeps surfacing the error. We never pre-disable intent from a read — a
    read can prove I *can* write but never that I *can't*; only the provider's
    denial is authoritative.
    """
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d")
    fake_client.upload_version_response = {
        "error": "AccessDenied: principal is not authorized to perform upload_version"
    }
    store.update("d", content="v2", snapshot=True)
    await publish_sync.push_version(store.get("d"))
    pub = store.get("d").publication
    assert pub.auto_sync is True  # intent preserved across a permission denial
    assert "sync failed" in pub.last_error.lower()


def test_is_pullable_content_type_allowlist():
    for ct in (
        "text/plain",
        "text/html",
        "text/markdown",
        "application/json",
        "image/svg+xml",
        "application/xml",
        "text/csv",
        "application/ld+json",
        "",
    ):
        assert publish_sync._is_pullable_content_type(ct) is True, ct
    for ct in (
        "image/png",
        "application/pdf",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        assert publish_sync._is_pullable_content_type(ct) is False, ct


@pytest.mark.asyncio
async def test_refresh_does_not_flag_upstream_ahead(store, fake_client):
    """A cloud-strictly-ahead version is a pullable edit, not drift-to-clobber."""
    art = store.create(name="Doc", content="hello", kind="text")
    await publish_sync.publish(art.slug, visibility="PRIVATE")
    fake_client.get_response = {
        "artifact": {
            "visibility": "PRIVATE",
            "sharedWith": [],
            "currentVersionNumber": 2,
            "sha256": "sha-ahead",
        }
    }
    refreshed = await publish_sync.refresh_publication(art.slug)
    assert refreshed.publication is not None
    assert refreshed.publication.last_error == ""


@pytest.mark.asyncio
async def test_pull_upstream_widget_unwraps_and_stays_widget(store, fake_client, tmp_path):
    """A Kiro Crew-published widget pulled back is unwrapped from its standalone
    document to the original inner fragment and STAYS a widget — so it remains
    inline-embeddable in chat (no html regression)."""
    art = store.create(name="W", content="<b>inner</b>", kind="widget")
    _track_publication(store, art.slug)
    # The remote bytes are what Kiro Crew uploads: the sentinel-wrapped doc.
    wrapped = publish_sync.wrap_widget_html("<b>collab edit</b>")
    fake_client.get_response = _remote_get(
        tmp_path, wrapped, version=2, sha="sha-v2", ctype="text/html"
    )
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    reloaded = store.get(art.slug)
    assert reloaded.kind == "widget"  # recovered fragment → still a widget
    assert reloaded.content == "<b>collab edit</b>"  # exact inner, unwrapped


def test_wrap_unwrap_widget_roundtrip():
    inner = '<div class="x">hi <span>there</span></div>'
    wrapped = publish_sync.wrap_widget_html(inner)
    assert wrapped.lstrip().lower().startswith("<!doctype")
    assert publish_sync.unwrap_widget_html(wrapped) == inner
    # Body injection appended outside the sentinels is excluded on unwrap.
    injected = wrapped.replace("</body>", "<script>/*provider*/</script></body>")
    assert publish_sync.unwrap_widget_html(injected) == inner
    # Sentinel-less / foreign document → not unwrappable.
    assert publish_sync.unwrap_widget_html("<!DOCTYPE html><html><body>x</body></html>") is None
    assert publish_sync.unwrap_widget_html("") is None
    # Idempotent: wrapping an already-standalone document does not double-wrap.
    assert publish_sync.wrap_widget_html(wrapped) == wrapped


@pytest.mark.asyncio
async def test_overwrite_upstream_forces_local_over_remote(store, fake_client, tmp_path):
    """Overwrite makes the local content the remote's new current version even
    when the remote moved ahead — WITHOUT pulling the remote's bytes locally.
    It adopts the remote's current token then force-pushes; the local content is
    untouched and auto_sync intent is preserved."""
    art = store.create(name="Doc", content="my version", kind="text")
    _track_publication(store, art.slug)  # last_pushed_sha256="sha-v1", synced v1
    # Remote moved ahead to v5 with a different sha (a collaborator's edit).
    fake_client.get_response = _remote_get(
        tmp_path, "their edit", version=5, sha="sha-remote-ahead"
    )
    result = await publish_sync.overwrite_upstream(art.slug)
    assert result["overwritten"] is True
    # The push adopted the remote's CURRENT token (force-overwrite), not the
    # stale one — proving we overrode the ahead remote rather than conflicting.
    uv = fake_client.called("upload_version")
    assert len(uv) == 1
    assert uv[0]["expected_current_sha256"] == "sha-remote-ahead"
    reloaded = store.get(art.slug)
    assert reloaded.content == "my version"  # remote bytes NOT pulled locally
    assert reloaded.publication.auto_sync is True  # intent preserved
    assert reloaded.publication.last_error == ""


@pytest.mark.asyncio
async def test_overwrite_upstream_rejects_non_publication(store, fake_client, tmp_path):
    """A fork (origin lineage only, no publication push target) can't be
    overwritten — there's nothing to push back to."""
    art = store.create(name="F", content="x", kind="html")
    store.set_fork_metadata(art.slug, ForkMetadata(upstream_artifact_id="uuid-123"))
    result = await publish_sync.overwrite_upstream(art.slug)
    assert result["overwritten"] is False
    assert "not a publication" in result["reason"]
    assert fake_client.called("upload_version") == []


@pytest.mark.asyncio
async def test_push_snapshots_live_dirty_before_push_no_map_drift(store, fake_client, tmp_path):
    """Pushing while live_dirty (e.g. an agent edited the live version without
    snapshotting, then Overwrite is invoked) must snapshot the live content into
    a REAL local version first, so the pushed remote version is backed by a local
    version whose bytes match. Reproduces the version_map<->content drift: before
    the fix, the live edit was pushed but the map keyed on the prior snapshot's
    number, whose stored bytes differed from what was pushed.
    """
    art = store.create(name="Doc", content="original", kind="text")  # v1
    _track_publication(store, art.slug)  # synced v1, version_map {"1": 1}
    # Agent edits the LIVE version, no snapshot -> still v1, but live_dirty.
    store.update(art.slug, content="blue live edit")
    assert store.get(art.slug).live_dirty is True
    assert store.get(art.slug).version == 1
    # Remote moved ahead from elsewhere; user clicks Overwrite.
    fake_client.get_response = _remote_get(
        tmp_path, "their edit", version=5, sha="sha-remote-ahead"
    )

    result = await publish_sync.overwrite_upstream(art.slug)
    assert result["overwritten"] is True

    reloaded = store.get(art.slug)
    pub = reloaded.publication
    # The live edit was captured as a real new local version (v2), not left
    # dangling in current.html.
    assert reloaded.version == 2
    assert reloaded.live_dirty is False
    assert reloaded.content == "blue live edit"
    # The map keys on the version that was actually pushed, and that version's
    # content matches the remote it points at — invariant intact, no drift.
    assert pub.last_synced_kirocrew_version == 2
    assert pub.version_map["2"] == fake_client.upload_version_response["versionNumber"]
    # No stale entry maps a prior version number whose bytes differ from what
    # was pushed (the pre-fix drift signature was version_map["1"] -> remote).
    assert "1" in pub.version_map and pub.version_map["1"] == 1
    assert result["local_version"] == 2
