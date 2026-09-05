"""Section markers must survive the two transcript COPY paths: fork and transfer.

Both paths filtered to ``("user", "assistant")``, so a copied transcript kept
every turn but lost every section boundary -- the worst shape of the bug, because
the copy looks complete.

The invariant is two-sided: markers are PRESERVED (with their ``meta["label"]``,
not just the rendered divider), and the fork's INDEX SPACE does not move, so
``at_message_index`` still counts user/assistant turns only.

The transfer half also pins wire compatibility: markers ride in an additive
top-level ``section_markers`` field, not inside ``messages``, because the importer
validates message roles strictly and an older peer would reject the whole bundle.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.session_directive_apply import SECTION_MARKER_ROLE
from kiro_crew.dashboard.session_transfer import (
    BUNDLE_VERSION,
    _validate_bundle,
    build_transfer_bundle_async,
)


def _marker(label: str = "Phase one") -> dict:
    return {
        "role": SECTION_MARKER_ROLE,
        "content": f"— End of: {label} —",
        "cls": "",
        "ts": "2026-09-04T10:00:00Z",
        "meta": {"label": label},
    }


class TestForkPreservesSectionMarkers:
    """The in-process copy path."""

    @pytest.mark.asyncio
    async def test_fork_copies_the_marker_and_its_label(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("markerfork")
        slot.append("user", "before", "msg msg-u")
        slot.append(SECTION_MARKER_ROLE, "— End of: Phase one —", "", meta={"label": "Phase one"})
        slot.append("assistant", "after", "msg msg-a")
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/markerfork/fork", json={})
            payload = await resp.json()

        assert resp.status == 200, f"fork failed: {payload}"
        forked = state._slots[payload["key"]]
        roles = [m.get("role") for m in forked.messages]
        assert SECTION_MARKER_ROLE in roles, (
            "the fork dropped the section marker: the copied transcript keeps every "
            f"turn but loses the boundary between them, roles={roles}"
        )
        marker = next(m for m in forked.messages if m.get("role") == SECTION_MARKER_ROLE)
        assert (marker.get("meta") or {}).get("label") == "Phase one", (
            "the marker survived but its label metadata did not, so the divider "
            f"renders unlabelled after a fork: meta={marker.get('meta')!r}"
        )

    @pytest.mark.asyncio
    async def test_a_marker_does_not_shift_the_fork_index_space(self, tmp_path, monkeypatch):
        """``at_message_index`` counts visible TURNS, markers excluded.

        The negative control for the fix: preserving markers must not smuggle them
        into the index space, or an index a caller already holds selects a
        different turn than it did before.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("markerindex")
        slot.append("user", "turn-0", "msg msg-u")
        slot.append(SECTION_MARKER_ROLE, "— End of: Phase one —", "", meta={"label": "Phase one"})
        slot.append("assistant", "turn-1", "msg msg-a")
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False

        async with TestClient(TestServer(_make_app(state))) as client:
            # Index 1 is the ASSISTANT turn (markers are not counted). Forking a
            # head at index 1 must therefore keep both turns.
            resp = await client.post(
                "/api/chat/slots/markerindex/fork", json={"at_message_index": 1}
            )
            payload = await resp.json()

        assert resp.status == 200, f"fork failed: {payload}"
        forked = state._slots[payload["key"]]
        contents = [m.get("content") for m in forked.messages]
        assert "turn-0" in contents and "turn-1" in contents, (
            "index 1 no longer names the assistant turn, so the marker entered the "
            f"fork index space and shifted the fork point: contents={contents}"
        )

    @pytest.mark.asyncio
    async def test_a_head_fork_keeps_the_marker_that_closes_its_last_turn(
        self, tmp_path, monkeypatch
    ):
        """A marker sits AFTER the turn whose section it closes, so a head slice
        ending at that turn must carry it. Cutting at ``position + 1`` drops it,
        silently moving the boundary the fork was supposed to preserve.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("headslice")
        slot.append("user", "turn-0", "msg msg-u")
        slot.append(SECTION_MARKER_ROLE, "— End of: Phase one —", "", meta={"label": "Phase one"})
        slot.append("assistant", "turn-1", "msg msg-a")
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False

        async with TestClient(TestServer(_make_app(state))) as client:
            # Head fork at the USER turn: the marker closing it must travel along.
            resp = await client.post("/api/chat/slots/headslice/fork", json={"at_message_index": 0})
            payload = await resp.json()

        assert resp.status == 200, f"fork failed: {payload}"
        forked = state._slots[payload["key"]]
        roles = [m.get("role") for m in forked.messages]
        contents = [m.get("content") for m in forked.messages]
        assert "turn-1" not in contents, f"head fork over-copied: contents={contents}"
        assert SECTION_MARKER_ROLE in roles, (
            "the head slice dropped the marker that closes its own last turn: "
            f"roles={roles} contents={contents}"
        )

    @pytest.mark.asyncio
    async def test_a_tail_fork_does_not_open_on_an_orphaned_marker(self, tmp_path, monkeypatch):
        """The mirror side: a tail starting at ``position + 1`` opens on the marker
        that closed the PREVIOUS turn, so the forked transcript begins with an
        "end of" rule for a section it does not contain.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.tail_fork_enabled = True
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.KiroCrewConfig.load", lambda: mock_cfg)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("tailslice")
        slot.append("user", "turn-0", "msg msg-u")
        slot.append(SECTION_MARKER_ROLE, "— End of: Phase one —", "", meta={"label": "Phase one"})
        slot.append("assistant", "turn-1", "msg msg-a")
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/tailslice/fork",
                json={"at_message_index": 0, "direction": "tail"},
            )
            payload = await resp.json()

        assert resp.status == 200, f"tail fork failed: {payload}"
        forked = state._slots[payload["key"]]
        roles = [m.get("role") for m in forked.messages]
        assert (
            roles and roles[0] != SECTION_MARKER_ROLE
        ), f"the tail opens on an orphaned section boundary: roles={roles}"
        assert "turn-1" in [m.get("content") for m in forked.messages]


class TestTransferPreservesSectionMarkers:
    """The cross-instance copy path."""

    @pytest.mark.asyncio
    async def test_bundle_carries_markers_out_of_band_with_label(self, tmp_path):
        from test_session_transfer import _slot, _state

        msgs = [
            {"role": "user", "content": "before", "cls": "msg msg-u", "ts": ""},
            _marker(),
            {"role": "assistant", "content": "after", "cls": "msg msg-a", "ts": ""},
        ]
        bundle = await build_transfer_bundle_async(_state(msgs), _slot(msgs), origin="mac")

        roles = [m["role"] for m in bundle["messages"]]
        assert SECTION_MARKER_ROLE not in roles, (
            "the marker was put INSIDE messages, where the importer validates roles "
            f"strictly -- an older peer would reject the whole bundle: roles={roles}"
        )
        markers = bundle.get("section_markers")
        assert markers, f"the bundle dropped the section marker entirely: {bundle.keys()}"
        assert (
            markers[0]["label"] == "Phase one"
        ), f"the marker travelled without its label: {markers[0]!r}"
        assert markers[0]["at"] == 1, (
            "the marker's insertion point must be its index in the visible message "
            f"list, so it lands between the same two turns: {markers[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_bundle_omits_the_field_when_there_are_no_markers(self, tmp_path):
        """Back-compat control: a marker-free session produces the OLD bundle shape."""
        from test_session_transfer import _slot, _state

        msgs = [{"role": "user", "content": "hi", "cls": "msg msg-u", "ts": ""}]
        bundle = await build_transfer_bundle_async(_state(msgs), _slot(msgs), origin="mac")
        assert "section_markers" not in bundle, (
            "an empty sidecar was emitted, changing the wire shape for every "
            "marker-free session rather than only for sessions that have markers"
        )

    def test_validate_accepts_markers_and_rejects_an_out_of_range_index(self):
        base = {
            "bundle_version": BUNDLE_VERSION,
            "title": "t",
            "origin": "mac",
            "messages": [{"role": "user", "content": "hi", "ts": ""}],
        }

        ok, err = _validate_bundle(
            {**base, "section_markers": [{"at": 1, "label": "L", "content": "— L —", "ts": ""}]}
        )
        assert err is None, f"a well-formed sidecar was rejected: {err}"
        assert ok["section_markers"][0]["label"] == "L"

        # ``at`` indexes the message list, so 2 is past the end of a 1-message
        # bundle. A peer-supplied index must be bounds-checked, not trusted.
        _, err = _validate_bundle(
            {**base, "section_markers": [{"at": 2, "label": "L", "content": "x", "ts": ""}]}
        )
        assert err is not None, "an out-of-range peer-supplied marker index was accepted"

    def test_validate_refuses_an_imported_label_the_schema_would_reject(self):
        """An imported label is schema-validated, not shortened to fit.

        A marker created through the tool passes ``SECTION_MARKER_SCHEMA``; an imported
        one never goes through creation, so truncating here would accept 121-500 chars
        and line-break control characters that the one-line rule cannot render.
        """
        from kiro_crew import validation

        base = {
            "bundle_version": BUNDLE_VERSION,
            "title": "t",
            "origin": "mac",
            "messages": [{"role": "user", "content": "hi", "ts": ""}],
        }
        cap = next(f.max_len for f in validation.SECTION_MARKER_SCHEMA.fields if f.name == "label")

        # Read off the schema rather than re-spelled, so the test cannot drift from it.
        _, err = _validate_bundle(
            {**base, "section_markers": [{"at": 1, "label": "x" * (cap + 1), "ts": ""}]}
        )
        assert err is not None, f"a label of {cap + 1} chars was accepted"

        for bad, what in ((("a\nb"), "newline"), ("a\u2028b", "line separator")):
            _, err = _validate_bundle(
                {**base, "section_markers": [{"at": 1, "label": bad, "ts": ""}]}
            )
            assert err is not None, f"a label carrying a {what} was accepted"

        # Positive control: exactly at the cap, no control characters, still imports.
        ok, err = _validate_bundle(
            {**base, "section_markers": [{"at": 1, "label": "y" * cap, "ts": ""}]}
        )
        assert err is None, f"a label at the cap was wrongly refused: {err}"
        assert ok["section_markers"][0]["label"] == "y" * cap, "the label was altered"

    def test_validate_tolerates_a_bundle_with_no_sidecar(self):
        """An older peer sends no such key at all; that is normal, not an error."""
        ok, err = _validate_bundle(
            {
                "bundle_version": BUNDLE_VERSION,
                "title": "t",
                "origin": "mac",
                "messages": [{"role": "user", "content": "hi", "ts": ""}],
            }
        )
        assert err is None, f"a v1/v2 bundle without the sidecar was rejected: {err}"
        assert ok["section_markers"] == []


class TestTransferCapCannotOverrunTheSlot:
    """The bundle cap and the slot cap must stay tied, not merely happen to match.

    A destination slot front-trims past ``_MAX_SLOT_MESSAGES``, and for rows not yet
    persisted that trim is silent and unrecoverable. A bundle contributes messages AND
    markers, so its worst case is twice its own cap -- which equalled the slot cap
    exactly, with zero margin, across two constants in different modules that nothing
    connected. This is where that connection fails loudly if either end moves.
    """

    def test_the_multiplier_matches_the_row_sources_the_bundle_actually_carries(self):
        """``_BUNDLE_ELEMENT_CLASSES`` must equal the row-contributing lists in a bundle.

        Asserting ``cap // n * n <= cap`` instead would be VACUOUS -- the derivation moves
        both sides together, so it holds for any ``n``. What can genuinely drift is a THIRD
        row source arriving while the multiplier stays 2, which silently halves the margin
        again. So the check counts the sources rather than re-doing the arithmetic.
        """
        from kiro_crew.dashboard.session_transfer import (
            _BUNDLE_ELEMENT_CLASSES,
            _validate_bundle,
        )

        ok, err = _validate_bundle(
            {
                "bundle_version": BUNDLE_VERSION,
                "title": "t",
                "origin": "mac",
                "messages": [{"role": "user", "content": "hi", "ts": ""}],
                "section_markers": [{"at": 1, "label": "L", "ts": ""}],
            }
        )
        assert err is None, f"the probe bundle was rejected: {err}"

        # Every value that becomes transcript ROWS in the destination slot. A scalar or a
        # non-row list (title, origin) is not one; both current sources are lists of rows.
        row_sources = {
            k for k, v in ok.items() if isinstance(v, list) and all(isinstance(e, dict) for e in v)
        }
        assert row_sources == {"messages", "section_markers"}, (
            f"the bundle's row sources changed to {sorted(row_sources)}; "
            f"_BUNDLE_ELEMENT_CLASSES must be updated with them"
        )
        assert _BUNDLE_ELEMENT_CLASSES == len(row_sources), (
            f"_BUNDLE_ELEMENT_CLASSES is {_BUNDLE_ELEMENT_CLASSES} but the bundle carries "
            f"{len(row_sources)} row source(s), so a maximal bundle overruns the slot cap"
        )

    def test_both_element_classes_are_bounded_by_that_one_cap(self):
        """The multiplier is only right while EACH class is capped at ``_MAX_MESSAGES``.

        Read off the module rather than restated: a second literal appearing on either
        guard is exactly the drift the derivation exists to prevent.
        """
        from kiro_crew.dashboard import session_transfer

        source = pathlib.Path(session_transfer.__file__).read_text(encoding="utf-8")
        assert source.count("len(raw_messages) > _MAX_MESSAGES") == 1, "message guard moved"
        assert source.count("len(raw_markers) > _MAX_MESSAGES") == 1, "marker guard moved"
        # The derivation itself, not a re-spelled number.
        assert (
            "_MAX_MESSAGES = _MAX_SLOT_MESSAGES // _BUNDLE_ELEMENT_CLASSES" in source
        ), "the bundle cap is no longer derived from the slot cap"


class TestProvenanceRefusalsShareOneSkeleton:
    """Two refusals, one gate: the shared clauses must come from one place.

    Pinned VERBATIM because the surrounding comment states that extending the
    directive set must not silently reword an unrelated tool's refusal. These
    assertions are what make that a checkable promise rather than an intention: a
    reword shows up here, while a pure factoring does not.
    """

    def test_the_surface_directive_refusal_is_unchanged(self):
        from kiro_crew.dashboard.session_directive_apply import _user_origin_refusal

        assert _user_origin_refusal(
            "set_project",
            "dashboard:x",
            " (dashboard or a messaging channel); headless callers such as "
            "cron jobs and sub-agents are refused",
        ) == (
            "Error: set_project only works from a user-facing session (dashboard "
            "or a messaging channel); headless callers such as cron jobs and "
            "sub-agents are refused (this turn is 'dashboard:x'). "
            "Nothing was changed."
        )

    def test_the_marker_refusal_is_unchanged(self):
        from kiro_crew.dashboard.session_directive_apply import _user_origin_refusal

        assert _user_origin_refusal(
            "section_marker",
            "dashboard:x",
            "; headless callers such as cron jobs, sub-agents and taskrunner "
            "turns are refused even when this session has an open dashboard tab",
        ) == (
            "Error: section_marker only works from a user-facing session; headless "
            "callers such as cron jobs, sub-agents and taskrunner turns are "
            "refused even when this session has an open dashboard tab (this "
            "turn is 'dashboard:x'). Nothing was changed."
        )


class TestSectionMarkerLabelIsRedactedAtBothBoundaries:
    """A label is a SECOND copy of caller text, so it needs the same scrub.

    Redacting the rendered divider content but not the label leaves the credential
    crossing the boundary in a field nobody looked at.
    """

    @pytest.mark.asyncio
    async def test_egress_redacts_the_label_not_only_the_content(self):
        from test_session_transfer import _slot, _state

        secret = "AKIAIOSFODNN7EXAMPLE"
        msgs = [
            {"role": "user", "content": "before", "cls": "msg msg-u", "ts": ""},
            {
                "role": SECTION_MARKER_ROLE,
                "content": f"— End of: {secret} —",
                "cls": "",
                "ts": "",
                "meta": {"label": secret},
            },
        ]
        bundle = await build_transfer_bundle_async(_state(msgs), _slot(msgs), origin="mac")

        marker = bundle["section_markers"][0]
        assert (
            secret not in marker["content"]
        ), f"the rendered content left the host unredacted: {marker['content']!r}"
        assert secret not in marker["label"], (
            "the label left the host unredacted while its own content was scrubbed, so "
            f"the secret crossed the egress boundary in the label field: {marker['label']!r}"
        )

    @pytest.mark.asyncio
    async def test_ingress_redacts_a_peer_supplied_label(self, tmp_path, monkeypatch):
        """The importer must not assume a well-behaved sender.

        A marker is never a user turn, so unlike a human's own words it carries no
        verbatim exemption on the way in.
        """
        from test_session_transfer import _make_request

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        secret = "AKIAIOSFODNN7EXAMPLE"
        state = _make_state(tmp_path)
        before = set(state._slots)

        body = {
            "bundle_version": BUNDLE_VERSION,
            "title": "imported",
            "origin": "peer",
            "messages": [{"role": "user", "content": "hi", "ts": ""}],
            "section_markers": [
                {"at": 1, "label": secret, "content": f"— End of: {secret} —", "ts": ""}
            ],
        }

        import kiro_crew.dashboard.session_transfer as st

        resp = await st.api_chat_slot_import(_make_request(state, body))
        assert resp.status == 200, f"import failed: {resp.text}"

        imported = next(state._slots[k] for k in set(state._slots) - before)
        marker = next(m for m in imported.messages if m.get("role") == SECTION_MARKER_ROLE)
        assert secret not in marker.get(
            "content", ""
        ), f"peer content landed unredacted: {marker.get('content')!r}"
        assert secret not in (marker.get("meta") or {}).get("label", ""), (
            "a peer-supplied label landed unredacted in this host's transcript: "
            f"{(marker.get('meta') or {}).get('label')!r}"
        )
