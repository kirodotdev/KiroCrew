"""A canvas owner can edit a canvas whose detail read masks some of its text.

The detail read masks every credential value (``_serialize``), so the editor holds
placeholders where the store holds the values and its autosave sends the whole
document back. ``restore_masked_values`` puts each value back under the placeholder
the save still carries, and refuses a save whose result would show a value the
read masks. Readers keep getting the masked copy whoever they are.
"""

from __future__ import annotations

import json
import re

import pytest
from artifacts_handlers_helpers import (  # noqa: F401 -- fixtures are requested by name
    _json_body,
    _request,
    isolated_store_fixture,
    patch_restricted_fixture,
)
from redaction_structured_cases import github_classic

from kiro_crew.dashboard.artifact_masked_merge import MaskedMergeError
from kiro_crew.dashboard.artifact_masked_merge import restore_masked_values as _restore
from kiro_crew.dashboard.handlers.artifacts import api_artifact_detail, api_artifact_update
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import _redact_text


def restore_masked_values(stored: str, incoming: str) -> str:
    return _restore(stored, incoming, _redact_text)


TAG = "[REDACTED: credential]"
ONE = github_classic(seed=36836)
TWO = github_classic(seed=36837)


def _node(node_id: str, text: str, x: int = 0) -> dict:
    return {
        "id": node_id,
        "type": "text",
        "x": x,
        "y": 0,
        "width": 200,
        "height": 120,
        "text": text,
    }


def _doc(*nodes: dict) -> str:
    return json.dumps({"kirocanvas": 1, "nodes": list(nodes), "edges": []}, indent=1)


def _texts(raw: str) -> dict[str, str]:
    return {n["id"]: n["text"] for n in json.loads(raw)["nodes"]}


def _edit(masked: str, **texts: str | None) -> str:
    """The page's save: *masked* with each named node's text replaced (None deletes it)."""
    doc = json.loads(masked)
    nodes = []
    for n in doc["nodes"]:
        if n["id"] in texts:
            if texts[n["id"]] is None:
                continue
            n = {**n, "text": texts[n["id"]]}
        nodes.append(n)
    known = {n["id"] for n in doc["nodes"]}
    nodes += [_node(i, t, 400) for i, t in texts.items() if i not in known and t is not None]
    return json.dumps({**doc, "nodes": nodes}, indent=1)


STORED = _doc(_node("a", f"token {ONE} end"), _node("b", "plain"), _node("c", f"token {TWO} too"))


def test_the_fixture_is_masked_by_the_real_read_filter() -> None:
    masked = _redact_text(STORED)
    assert ONE not in masked and TWO not in masked
    assert _texts(masked)["a"] == f"token {TAG} end"


class TestRestore:
    def test_an_unchanged_masked_save_keeps_every_value(self) -> None:
        out = restore_masked_values(STORED, _redact_text(STORED))
        assert _texts(out) == _texts(STORED)

    def test_editing_around_masked_text_keeps_it(self) -> None:
        save = _edit(_redact_text(STORED), a=f"Note: token {TAG} end, ship it")
        assert _texts(restore_masked_values(STORED, save))["a"] == f"Note: token {ONE} end, ship it"

    def test_editing_unmasked_text_leaves_masked_shapes_whole(self) -> None:
        save = _edit(_redact_text(STORED), b="changed", d="new sticky")
        texts = _texts(restore_masked_values(STORED, save))
        assert texts == {**_texts(STORED), "b": "changed", "d": "new sticky"}

    def test_removing_the_only_placeholder_of_a_text_deletes_its_value(self) -> None:
        save = _edit(_redact_text(STORED), a="token end")
        out = restore_masked_values(STORED, save)
        assert ONE not in out and TWO in out

    def test_deleting_a_masked_shape_deletes_its_value(self) -> None:
        out = restore_masked_values(STORED, _edit(_redact_text(STORED), a=None))
        assert ONE not in out and _texts(out)["c"] == f"token {TWO} too"

    def test_mixed_edits_keep_each_value_in_its_own_shape(self) -> None:
        stored = _doc(_node("a", f"token {ONE} and token {TWO}"), _node("b", "plain"))
        save = _edit(_redact_text(stored), a=f"first: token {TAG} and token {TAG}!", b="edited")
        texts = _texts(restore_masked_values(stored, save))
        assert texts == {"a": f"first: token {ONE} and token {TWO}!", "b": "edited"}

    def test_removing_one_of_several_placeholders_in_a_text_is_refused(self) -> None:
        stored = _doc(_node("a", f"token {ONE} and token {TWO}"))
        for text in (f"token {TAG}", f"and token {TAG}", f"{TAG} {TAG} {TAG}"):
            with pytest.raises(MaskedMergeError):
                restore_masked_values(stored, _edit(_redact_text(stored), a=text))

    def test_reordering_shapes_keeps_values_with_their_shape(self) -> None:
        doc = json.loads(_redact_text(STORED))
        doc["nodes"].reverse()
        out = restore_masked_values(STORED, json.dumps(doc, indent=1))
        assert _texts(out) == _texts(STORED)

    def test_undoing_the_deletion_of_the_last_hidden_text_is_refused(self) -> None:
        stored = _doc(_node("a", f"token {ONE}"), _node("b", "plain"))
        masked = _redact_text(stored)
        after_delete = restore_masked_values(stored, _edit(masked, a=None))
        assert ONE not in after_delete and not re.search(r"REDACTED", after_delete)
        with pytest.raises(MaskedMergeError):
            restore_masked_values(after_delete, masked)

    def test_a_credential_typed_next_to_masked_text_saves_and_then_reads_back_masked(self) -> None:
        stored = _doc(_node("a", f"token {ONE} end"))
        save = _edit(_redact_text(stored), a=f"token {TAG} end {TWO}")
        first = restore_masked_values(stored, save)
        assert _texts(first)["a"] == f"token {ONE} end {TWO}"
        # The page adopts the masked reply, so its next save matches the stored copy.
        reply = _redact_text(first)
        assert _texts(reply)["a"] == f"token {TAG} end {TAG}"
        again = restore_masked_values(first, _edit(reply, b="next"))
        assert _texts(again) == {"a": f"token {ONE} end {TWO}", "b": "next"}

    def test_a_placeholder_cut_into_another_shape_is_refused(self) -> None:
        save = _edit(_redact_text(STORED), a="end", d=f"token {TAG}")
        with pytest.raises(MaskedMergeError):
            restore_masked_values(STORED, save)

    def test_a_copied_placeholder_is_refused_not_stored_as_text(self) -> None:
        save = _edit(_redact_text(STORED), d=f"token {TAG} end")
        with pytest.raises(MaskedMergeError):
            restore_masked_values(STORED, save)

    def test_undoing_a_deletion_that_was_already_saved_is_refused(self) -> None:
        masked = _redact_text(STORED)
        after_delete = restore_masked_values(STORED, _edit(masked, a=None))
        assert ONE not in after_delete
        with pytest.raises(MaskedMergeError):
            restore_masked_values(after_delete, masked)

    def test_a_placeholder_moved_inside_its_own_text_is_refused(self) -> None:
        stored = _doc(_node("a", f"first {ONE} second {TWO} end"))
        for text in (f"{TAG} first second {TAG} end", f"second {TAG} first {TAG} end"):
            with pytest.raises(MaskedMergeError):
                restore_masked_values(stored, _edit(_redact_text(stored), a=text))

    def test_a_deleted_value_never_lands_in_text_that_only_looks_like_a_placeholder(self) -> None:
        stored = _doc(_node("a", f"token {ONE}"), _node("x", f"agent wrote {TAG}"))
        masked = _redact_text(stored)
        out = restore_masked_values(stored, _edit(masked, a=None))
        assert ONE not in out and _texts(out) == {"x": f"agent wrote {TAG}"}
        out = restore_masked_values(stored, _edit(masked, a="token gone"))
        assert ONE not in out and _texts(out)["x"] == f"agent wrote {TAG}"

    def test_plain_placeholder_text_is_editable_and_not_a_value(self) -> None:
        stored = _doc(_node("a", f"token {ONE}"), _node("x", f"agent wrote {TAG}"))
        masked = _redact_text(stored)
        out = restore_masked_values(stored, _edit(masked, x=f"still {TAG}, edited", b="new"))
        assert _texts(out) == {"a": f"token {ONE}", "x": f"still {TAG}, edited", "b": "new"}
        with pytest.raises(MaskedMergeError):
            restore_masked_values(stored, _edit(masked, x=f"{TAG} {TAG}"))

    def test_a_long_masked_text_is_merged_quickly_when_only_its_ends_change(self) -> None:
        body = "lorem ipsum " * 3000
        stored = _doc(_node("a", f"{body}{ONE}{body}{TWO}{body}"))
        save = _edit(_redact_text(stored), a=f"start {body}{TAG}{body}{TAG}{body} end")
        assert _texts(restore_masked_values(stored, save))["a"] == (
            f"start {body}{ONE}{body}{TWO}{body} end"
        )

    def test_editing_the_text_between_two_placeholders_is_refused_not_guessed(self) -> None:
        stored = _doc(_node("a", f"first {ONE} second {TWO} end"))
        save = _edit(_redact_text(stored), a=f"first {TAG} second, edited {TAG} end")
        with pytest.raises(MaskedMergeError):
            restore_masked_values(stored, save)

    def test_editing_either_side_of_a_single_placeholder_is_merged(self) -> None:
        stored = _doc(_node("a", f"before {ONE} after"))
        save = _edit(_redact_text(stored), a=f"much more before {TAG} and after")
        assert _texts(restore_masked_values(stored, save))["a"] == (
            f"much more before {ONE} and after"
        )

    def test_a_credential_inside_a_link_keeps_the_link_around_it(self) -> None:
        stored = _doc(_node("a", f"see https://example.test/?data={ONE}&page=2"))
        masked = _redact_text(stored)
        assert ONE not in masked and "https://example.test/?data=" in masked
        save = _edit(masked, a=f"read https://example.test/?data={TAG}&page=3")
        assert _texts(restore_masked_values(stored, save))["a"] == (
            f"read https://example.test/?data={ONE}&page=3"
        )

    def test_a_non_ascii_and_escaped_document_round_trips(self) -> None:
        stored = _doc(_node("a", f'naïve "quoted"\nline {ONE} 日本語'), _node("b", "é"))
        save = _edit(_redact_text(stored), b="é!")
        texts = _texts(restore_masked_values(stored, save))
        assert texts == {"a": f'naïve "quoted"\nline {ONE} 日本語', "b": "é!"}

    def test_an_escaped_lone_surrogate_stays_escaped_and_storable(self) -> None:
        stored = _doc(_node("a", f"token {ONE}"), _node("b", "lone \ud800 half"))
        save = _edit(_redact_text(stored), a=f"edited {TAG}")
        out = restore_masked_values(stored, save)
        assert "\\ud800" in out and "\ud800" not in out
        out.encode("utf-8")
        assert _texts(out)["a"] == f"edited {ONE}"

    def test_a_save_without_placeholders_is_written_as_sent(self) -> None:
        save = _edit(_redact_text(STORED), a="x", c="y")
        assert restore_masked_values(STORED, save) == save

    def test_nothing_hidden_means_placeholders_are_plain_text(self) -> None:
        stored = _doc(_node("a", f"literal {TAG}"))
        save = _edit(stored, a=f"literal {TAG}!")
        assert restore_masked_values(stored, save) == save

    def test_a_value_the_save_would_unmask_is_refused(self) -> None:
        # A filter that masks a value only behind its label: dropping the label
        # would show the value, so nothing may be written.
        def label_only(text: str) -> str:
            return re.sub(r"key=\w+", f"key={TAG}", text)

        stored = _doc(_node("a", "key=hiddenvalue"))
        assert _restore(stored, _doc(_node("a", f"key={TAG} ok")), label_only)
        with pytest.raises(MaskedMergeError):
            _restore(stored, _doc(_node("a", f"{TAG}")), label_only)


class TestRoute:
    """The PATCH route merges for the owner's canvas save and nowhere else."""

    @staticmethod
    async def _canvas(store) -> tuple[str, str]:
        store.create(name="board", content=STORED, slug="board", kind="canvas")
        detail = _json_body(await api_artifact_detail(_request(match={"slug": "board"})))
        return detail["content"], detail["content_token"]

    @pytest.mark.asyncio
    async def test_owner_edits_a_masked_canvas_and_the_values_stay_hidden(
        self, isolated_store, patch_restricted
    ) -> None:
        masked, token = await self._canvas(isolated_store)
        assert ONE not in masked
        save = _edit(masked, b="edited", a=f"token {TAG} end!")
        resp = await api_artifact_update(
            _request(
                body={"content": save, "snapshot": False, "expected_token": token},
                match={"slug": "board"},
            )
        )
        assert resp.status == 200
        assert ONE.encode() not in resp.body and TWO.encode() not in resp.body
        assert _texts(isolated_store.get("board").content) == {
            "a": f"token {ONE} end!",
            "b": "edited",
            "c": f"token {TWO} too",
        }
        # Every reader still gets the masked copy: the dashboard and an agent.
        for headers in (None, {"X-Internal-Secret": "x"}):
            read = await api_artifact_detail(
                _request(match={"slug": "board"}, extra_headers=headers)
            )
            assert ONE.encode() not in read.body and TWO.encode() not in read.body
            assert _json_body(read)["content_redacted"] is True

    @pytest.mark.asyncio
    async def test_a_stale_save_is_not_merged_and_not_written(
        self, isolated_store, patch_restricted
    ) -> None:
        masked, _token = await self._canvas(isolated_store)
        resp = await api_artifact_update(
            _request(body={"content": masked, "expected_token": "0" * 64}, match={"slug": "board"})
        )
        assert resp.status == 409
        assert isolated_store.get("board").content == STORED

    @pytest.mark.asyncio
    async def test_a_save_that_would_unmask_is_refused_400(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts_parts import create_update

        masked, token = await self._canvas(isolated_store)

        def refuse(*_args: str) -> str:
            raise MaskedMergeError("would show")

        monkeypatch.setattr(create_update, "restore_masked_values", refuse)
        resp = await api_artifact_update(
            _request(body={"content": masked, "expected_token": token}, match={"slug": "board"})
        )
        assert resp.status == 400
        assert "nothing was written" in _json_body(resp)["error"]
        assert _json_body(resp)["code"] == "masked_merge_refused"
        assert isolated_store.get("board").content == STORED

    @pytest.mark.asyncio
    async def test_placeholders_from_an_older_version_are_refused_400(
        self, isolated_store, patch_restricted
    ) -> None:
        masked, token = await self._canvas(isolated_store)
        # "Keep mine" after a 409: the save names the live token, but its
        # placeholders came from the read before it.
        resp = await api_artifact_update(
            _request(
                body={"content": masked, "expected_token": token, "placeholders_token": "0" * 64},
                match={"slug": "board"},
            )
        )
        assert resp.status == 400
        assert _json_body(resp)["code"] == "masked_merge_stale"
        assert isolated_store.get("board").content == STORED
        same = await api_artifact_update(
            _request(
                body={"content": masked, "expected_token": token, "placeholders_token": token},
                match={"slug": "board"},
            )
        )
        assert same.status == 200
        assert isolated_store.get("board").content == STORED

    @pytest.mark.asyncio
    async def test_an_agent_write_is_not_merged(self, isolated_store, patch_restricted) -> None:
        masked, token = await self._canvas(isolated_store)
        resp = await api_artifact_update(
            _request(
                body={"content": masked, "expected_token": token},
                match={"slug": "board"},
                extra_headers={"X-Internal-Secret": "x"},
            )
        )
        assert resp.status == 200
        assert ONE not in isolated_store.get("board").content

    @pytest.mark.asyncio
    async def test_other_kinds_are_written_as_sent(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="doc", content=f"token {ONE} end", slug="doc", kind="markdown")
        token = isolated_store.get("doc").content_token
        resp = await api_artifact_update(
            _request(
                body={"content": f"token {TAG} end", "expected_token": token},
                match={"slug": "doc"},
            )
        )
        assert resp.status == 200
        assert isolated_store.get("doc").content == f"token {TAG} end"
