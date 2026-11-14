"""``room_principal``: the principal of a person the Kiro Space app attested.

Built only from the ``{userId, displayName}`` the app-mapped webhook token carried,
never from any other request field.
"""

from __future__ import annotations

from kiro_crew.shared_crew.contracts import Principal, PrincipalKind, Provenance
from kiro_crew.shared_crew.identity import (
    local_operator_principal,
    room_principal,
    tailnet_principal,
)


def test_the_room_provenance_exists():
    assert Provenance.KIRO_SPACE_ROOM.value == "kiro_space_room"


def test_room_principal_is_a_human_keyed_by_the_user_id():
    p = room_principal("usr_bob", "Bob")
    assert p.kind is PrincipalKind.HUMAN
    assert p.provenance is Provenance.KIRO_SPACE_ROOM
    assert p.subject == "usr_bob"
    assert p.display.name == "Bob"
    # A login is never carried: a stored stamp loses its provenance.
    assert p.display.login == ""


def test_the_id_follows_the_user_id_only_not_the_display_name():
    assert room_principal("usr_bob", "Bob").id == room_principal("usr_bob", "Robert").id
    assert room_principal("usr_bob", "Bob").id != room_principal("usr_eve", "Bob").id


def test_a_room_user_never_shares_an_id_with_a_local_operator_or_tailnet_login():
    same = "alice"
    ids = {
        room_principal(same, "A").id,
        local_operator_principal(same).id,
        tailnet_principal(same, "node", Provenance.TAILSCALE_SERVE).id,
    }
    assert len(ids) == 3


def test_an_empty_user_id_is_unknown_never_an_anonymous_person():
    assert room_principal("", "Bob").is_unknown
    assert room_principal("   ", "Bob") == Principal.unknown()


def test_the_display_name_is_bounded_and_a_blank_one_gets_a_neutral_label():
    assert len(room_principal("usr_bob", "x" * 500).display.name) <= 128
    name = room_principal("usr_bob", "").display.name
    assert name and "usr_bob" not in name


def test_a_room_principal_is_never_the_local_operator():
    assert room_principal("usr_bob", "Bob").provenance is not Provenance.LOCAL_OPERATOR
