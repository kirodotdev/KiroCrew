"""The room principal: built only from the identity the room verified on join.

``room_principal`` takes the userId and display name from the join handshake
(``RoomIdentity``). A frame field never names a principal.
"""

from __future__ import annotations

from kiro_crew.shared_crew.contracts import Principal, PrincipalKind, Provenance
from kiro_crew.shared_crew.identity import (
    RoomIdentity,
    derive_principal,
    room_principal,
)


def _derive(**over):
    args = dict(
        internal=False,
        app="",
        token_subject="owner-token",
        serve_peer=None,
        direct_peer=None,
        remote="127.0.0.1",
        forwarded=False,
    )
    args.update(over)
    return derive_principal(**args)


def test_the_room_provenance_exists():
    assert Provenance.KIRO_SPACE_ROOM.value == "kiro_space_room"


def test_room_principal_is_a_human_keyed_by_the_verified_user_id():
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
    from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal

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


def test_the_display_name_is_bounded():
    assert len(room_principal("usr_bob", "x" * 500).display.name) <= 128


def test_a_blank_display_name_falls_back_to_a_neutral_label():
    name = room_principal("usr_bob", "").display.name
    assert name and "usr_bob" not in name


def test_derive_principal_prefers_the_room_identity_over_loopback_and_tailnet():
    room = RoomIdentity(user_id="usr_bob", display_name="Bob")
    assert _derive(room_identity=room).provenance is Provenance.KIRO_SPACE_ROOM
    # Even a loopback socket with the owner's token does not outrank the verified room peer
    # (the bridge's connection is not a request; no request may reach this branch).
    assert _derive(room_identity=None).provenance is Provenance.LOCAL_OPERATOR


def test_app_and_internal_evidence_still_outrank_the_room_identity():
    room = RoomIdentity(user_id="usr_bob", display_name="Bob")
    assert _derive(room_identity=room, app="notes").kind is PrincipalKind.SYSTEM
    assert _derive(room_identity=room, internal=True).kind is PrincipalKind.SYSTEM


def test_derive_principal_without_a_room_identity_is_unchanged():
    assert _derive().provenance is Provenance.LOCAL_OPERATOR
    assert _derive(remote="203.0.113.9").is_unknown
