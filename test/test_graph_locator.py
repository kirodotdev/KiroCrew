"""Tests for the Microsoft Graph resource locator (pure logic).

The app-only ``/me`` refusal is the load-bearing negative test: it is a hard
Graph vendor constraint enforced at shaping time, so it must fail closed.
"""

import pytest

from kiro_crew.connections.control_plane.operation import (
    CREDENTIAL_MODE_FINE_GRAINED_PAT,
    CREDENTIAL_MODE_OAUTH_USER,
    CREDENTIAL_MODE_SERVICE_TO_SERVICE,
)
from kiro_crew.connections.vendors.microsoft.graph.locator import (
    GraphLocator,
    GraphLocatorError,
    Principal,
    ResourceRef,
    build_path,
    is_app_only,
)


class TestCredentialModeAppOnlyPredicate:
    def test_service_to_service_is_app_only(self):
        assert is_app_only(CREDENTIAL_MODE_SERVICE_TO_SERVICE) is True

    def test_delegated_credentials_are_not_app_only(self):
        # Both user-backed credential modes have a signed-in user, so /me resolves.
        assert is_app_only(CREDENTIAL_MODE_OAUTH_USER) is False
        assert is_app_only(CREDENTIAL_MODE_FINE_GRAINED_PAT) is False


class TestPrincipalRootAndCredentialMode:
    def test_me_resolves_under_oauth_user(self):
        loc = GraphLocator(principal=Principal.ME, resource=ResourceRef.onenote(tail=["notebooks"]))
        assert build_path(loc, CREDENTIAL_MODE_OAUTH_USER) == "/me/onenote/notebooks"

    def test_me_resolves_under_fine_grained_pat(self):
        loc = GraphLocator(principal=Principal.ME, resource=ResourceRef.onenote(tail=["notebooks"]))
        assert build_path(loc, CREDENTIAL_MODE_FINE_GRAINED_PAT) == "/me/onenote/notebooks"

    def test_me_is_refused_under_service_to_service(self):
        # The hard vendor constraint: an app-only (service_to_service) credential
        # has no signed-in user, so /me is unresolvable. Enforced at shaping
        # time, not discovered as a 400.
        loc = GraphLocator(principal=Principal.ME, resource=ResourceRef.onenote(tail=["notebooks"]))
        with pytest.raises(GraphLocatorError, match="app-only"):
            build_path(loc, CREDENTIAL_MODE_SERVICE_TO_SERVICE)

    def test_users_id_resolves_under_service_to_service(self):
        loc = GraphLocator(
            principal=Principal.USER,
            user_id="user-123",
            resource=ResourceRef.onenote(tail=["notebooks"]),
        )
        assert (
            build_path(loc, CREDENTIAL_MODE_SERVICE_TO_SERVICE)
            == "/users/user-123/onenote/notebooks"
        )

    def test_users_id_resolves_under_oauth_user_too(self):
        loc = GraphLocator(
            principal=Principal.USER,
            user_id="user-123",
            resource=ResourceRef.onenote(tail=["pages"]),
        )
        assert build_path(loc, CREDENTIAL_MODE_OAUTH_USER) == "/users/user-123/onenote/pages"

    def test_user_principal_requires_id(self):
        with pytest.raises(GraphLocatorError, match="user_id"):
            GraphLocator(principal=Principal.USER, resource=ResourceRef.onenote(tail=["pages"]))

    def test_user_id_with_me_principal_is_rejected(self):
        with pytest.raises(GraphLocatorError, match="only valid when principal is USER"):
            GraphLocator(
                principal=Principal.ME,
                user_id="user-123",
                resource=ResourceRef.onenote(tail=["pages"]),
            )

    def test_empty_user_id_is_rejected(self):
        with pytest.raises(GraphLocatorError, match="non-empty"):
            GraphLocator(
                principal=Principal.USER,
                user_id="   ",
                resource=ResourceRef.onenote(tail=["pages"]),
            )


class TestTopLevelShapesIgnorePrincipalAndCredentialMode:
    # A top-level (not principal-rooted) ref never touches the principal, so the
    # app-only /me refusal cannot apply to it -- proven by building it under a
    # service_to_service credential with a ME principal and getting a valid path.

    def test_sharepoint_list_items_under_service_to_service_with_me(self):
        loc = GraphLocator(
            principal=Principal.ME,
            resource=ResourceRef.sharepoint_list_items(site_id="s1", list_id="l1"),
        )
        assert build_path(loc, CREDENTIAL_MODE_SERVICE_TO_SERVICE) == "/sites/s1/lists/l1/items"

    def test_chat_messages_under_service_to_service_with_me(self):
        loc = GraphLocator(
            principal=Principal.ME,
            resource=ResourceRef.chat_messages(chat_id="c1"),
        )
        assert build_path(loc, CREDENTIAL_MODE_SERVICE_TO_SERVICE) == "/chats/c1/messages"


class TestDirectResourceRefConstruction:
    def test_valid_segments_succeed(self):
        ref = ResourceRef(("chats", "chat-1", "messages"), principal_rooted=False)
        assert ref.segments == ("chats", "chat-1", "messages")

    def test_segment_with_slash_is_rejected(self):
        with pytest.raises(GraphLocatorError, match="path separator"):
            ResourceRef(("bad/seg",), principal_rooted=False)

    def test_empty_segments_are_rejected(self):
        with pytest.raises(GraphLocatorError, match="at least one segment"):
            ResourceRef((), principal_rooted=False)


class TestSharepointListItems:
    def test_collection(self):
        ref = ResourceRef.sharepoint_list_items(site_id="s1", list_id="l1")
        assert ref.segments == ("sites", "s1", "lists", "l1", "items")

    def test_specific_item(self):
        ref = ResourceRef.sharepoint_list_items(site_id="s1", list_id="l1", item_id="i1")
        assert ref.segments == ("sites", "s1", "lists", "l1", "items", "i1")

    def test_item_fields(self):
        ref = ResourceRef.sharepoint_list_items(
            site_id="s1", list_id="l1", item_id="i1", fields=True
        )
        assert ref.segments[-2:] == ("i1", "fields")

    def test_fields_without_item_is_rejected(self):
        with pytest.raises(GraphLocatorError, match="fields requires an item_id"):
            ResourceRef.sharepoint_list_items(site_id="s1", list_id="l1", fields=True)

    def test_empty_site_id_is_rejected(self):
        with pytest.raises(GraphLocatorError, match="site_id"):
            ResourceRef.sharepoint_list_items(site_id="", list_id="l1")

    def test_id_with_slash_is_rejected(self):
        # A slash would inject unmodelled path structure.
        with pytest.raises(GraphLocatorError, match="path separator"):
            ResourceRef.sharepoint_list_items(site_id="s1/evil", list_id="l1")


class TestDriveItem:
    def test_plain_item(self):
        ref = ResourceRef.drive_item(drive_id="d1", item_id="it1")
        assert ref.segments == ("drives", "d1", "items", "it1")

    def test_children(self):
        ref = ResourceRef.drive_item(drive_id="d1", item_id="it1", children=True)
        assert ref.segments[-1] == "children"

    def test_workbook_tail(self):
        ref = ResourceRef.drive_item(
            drive_id="d1", item_id="it1", workbook_tail=["workbook", "worksheets"]
        )
        assert ref.segments[-2:] == ("workbook", "worksheets")

    def test_children_and_workbook_are_mutually_exclusive(self):
        with pytest.raises(GraphLocatorError, match="mutually exclusive"):
            ResourceRef.drive_item(
                drive_id="d1", item_id="it1", children=True, workbook_tail=["workbook"]
            )

    def test_workbook_tail_must_start_with_workbook(self):
        with pytest.raises(GraphLocatorError, match="must start with 'workbook'"):
            ResourceRef.drive_item(drive_id="d1", item_id="it1", workbook_tail=["worksheets"])


class TestOneNoteAndChat:
    def test_onenote_tail(self):
        ref = ResourceRef.onenote(tail=["pages", "p1", "content"])
        assert ref.segments == ("onenote", "pages", "p1", "content")
        assert ref.principal_rooted is True

    def test_chat_messages(self):
        ref = ResourceRef.chat_messages(chat_id="c1")
        assert ref.segments == ("chats", "c1", "messages")
        assert ref.principal_rooted is False

    def test_empty_chat_id_is_rejected(self):
        with pytest.raises(GraphLocatorError, match="chat_id"):
            ResourceRef.chat_messages(chat_id="")
