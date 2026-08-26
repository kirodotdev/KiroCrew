"""Tests for PublishProvider capability negotiation and comment interface."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew import publish_provider as pp
from kiro_crew.publish_provider import (
    Capability,
    CapabilityNotSupportedError,
    CommentAnchor,
    PublishProvider,
    RemoteComment,
    list_providers,
    register_provider,
    reset_providers,
)


class MinimalProvider(PublishProvider):
    """Provider with only content + sharing (no comments)."""

    name = "minimal"
    install_hint = ""

    def available(self) -> bool:
        return True

    def view_url_for(self, external_id: str) -> str:
        return f"https://example.com/{external_id}"

    async def publish(self, **kwargs):
        pass  # pragma: no cover

    async def push_version(self, **kwargs):
        pass  # pragma: no cover

    async def update_sharing(self, **kwargs):
        pass  # pragma: no cover

    async def unpublish(self, **kwargs):
        pass  # pragma: no cover


class TestCapabilityNegotiation:
    def test_default_capabilities(self):
        p = MinimalProvider()
        caps = p.capabilities()
        assert Capability.CONTENT_VERSIONS in caps
        assert Capability.SHARING in caps
        assert Capability.COMMENTS_READ not in caps

    @pytest.mark.asyncio
    async def test_fetch_comments_raises_unsupported(self):
        p = MinimalProvider()
        with pytest.raises(CapabilityNotSupportedError) as exc_info:
            await p.fetch_comments(external_id="abc")
        assert "comments_read" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_post_comment_raises_unsupported(self):
        p = MinimalProvider()
        with pytest.raises(CapabilityNotSupportedError):
            await p.post_comment(external_id="abc", body="hi")

    @pytest.mark.asyncio
    async def test_reply_comment_raises_unsupported(self):
        p = MinimalProvider()
        with pytest.raises(CapabilityNotSupportedError):
            await p.reply_comment(external_id="abc", parent_remote_id="x", body="hi")

    @pytest.mark.asyncio
    async def test_mark_review_raises_unsupported(self):
        p = MinimalProvider()
        with pytest.raises(CapabilityNotSupportedError):
            await p.mark_review(external_id="abc", remote_id="x")

    @pytest.mark.asyncio
    async def test_delete_comment_raises_unsupported(self):
        p = MinimalProvider()
        with pytest.raises(CapabilityNotSupportedError):
            await p.delete_comment(external_id="abc", remote_id="x")


class TestListProviders:
    def test_list_registered(self):
        # In the public edition NO concrete provider is registered — the registry
        # is empty until a companion (or a test) registers one. Register a dummy
        # provider so list_providers() returns something to assert on, then fully
        # restore the global registry (reset_providers only drops instances).
        saved_factories = dict(pp._FACTORIES)
        try:
            register_provider("minimal", MinimalProvider)
            providers = list_providers()
            assert len(providers) >= 1
            names = [p.name for p in providers]
            assert "minimal" in names
        finally:
            pp._FACTORIES.clear()
            pp._FACTORIES.update(saved_factories)
            reset_providers()


class TestInstallable:
    def test_abc_default_is_not_installable(self):
        # A provider without a self-install story stays hidden when unavailable.
        assert PublishProvider.installable(MagicMock(spec=PublishProvider)) is False

    def test_minimal_provider_is_not_installable(self):
        assert MinimalProvider().installable() is False


class TestCommentAnchor:
    def test_defaults(self):
        a = CommentAnchor()
        assert a.quote is None
        assert a.start_offset is None

    def test_with_values(self):
        a = CommentAnchor(quote="hello", start_offset=5, end_offset=10, version_number=3)
        assert a.quote == "hello"
        assert a.version_number == 3


class TestRemoteComment:
    def test_defaults(self):
        rc = RemoteComment(remote_id="r1", thread_id="r1", author="bob", body="hi")
        assert rc.status == "open"
        assert rc.deleted is False
        assert rc.is_agent is False
        assert rc.anchor is None
