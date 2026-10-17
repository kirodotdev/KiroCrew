"""The slot-detail render reuses unchanged rows without changing a byte of output.

``wire_render.prepare_messages_json`` must return exactly
``json.dumps(_prepare_messages(...))`` for every input, and must render a row
again whenever anything the display redaction reads has changed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard import wire_render
from kiro_crew.history import _safe_key
from kiro_crew.security import exfil
from kiro_crew.security.redaction_switch import owner_view

SECRET = "AKIAIOSFODNN7EXAMPLE"
TENANT_URL = (
    "https://tenant.example.com/collect?data=c2VjcmV0LXRva2VuLXZhbHVlLTEyMzQ1Njc4OTAxMjM0NTY3ODkw"
)


@pytest.fixture(autouse=True)
def _cold_caches():
    cu._clear_display_redaction_cache()
    wire_render.clear_render_memo()
    yield
    cu._clear_display_redaction_cache()
    wire_render.clear_render_memo()


def _rows() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": f"my key is {SECRET}", "ts": "t0", "meta": {"mid": "u0"}},
        {
            "role": "assistant",
            "content": f"found {SECRET} and see {TENANT_URL} done",
            "ts": "t1",
            "meta": {"mid": "a1", "tool": {"name": "grep", "title": f"grep {SECRET}"}},
        },
        {"role": "system", "content": f"system {SECRET}", "ts": "t2"},
        {"role": "assistant", "content": ["structured", {"k": SECRET}], "ts": "t3"},
        {
            "role": "assistant",
            "content": "v2",
            "ts": "t4",
            "variants": [{"content": f"v1 {SECRET}", "ts": "t4a"}],
        },
        {
            "role": "mcp_oauth",
            "content": "",
            "ts": "t5",
            "meta": {"oauth_url": "https://auth.example.com/authorize?x=1", "child": "c-1"},
        },
        {"role": "permission", "content": "allow?", "cls": "msg msg-p", "ts": "t6"},
        {"role": "chunk", "content": "part one ", "seq": 1},
        {"role": "chunk", "content": f"part two {SECRET}", "seq": 2},
        {"role": "done", "content": ""},
        {"role": "assistant", "content": "", "ts": "t7"},
        {"role": "assistant", "content": "tail", "ts": "t8", "meta": {"n": 1.5, "flag": True}},
    ]


def _expected(rows: list[dict], live_child: str = "c-1", workspace: str | None = None) -> str:
    return json.dumps(cu._prepare_messages(rows, False, live_child=live_child, workspace=workspace))


def _actual(rows: list[dict], live_child: str = "c-1", workspace: str | None = None) -> str:
    return wire_render.prepare_messages_json(
        rows, False, live_child=live_child, workspace=workspace
    )


class TestSameBytesAsTheUnmemoizedRender:
    def test_cold_and_warm_renders_match_json_dumps_of_prepare_messages(self) -> None:
        expected = _expected(_rows())
        assert _actual(_rows()) == expected
        assert wire_render.render_memo_info()[0] > 0
        assert _actual(_rows()) == expected
        assert SECRET not in expected.replace(f"my key is {SECRET}", "")

    def test_a_row_json_cannot_encode_is_rendered_without_the_memo(self) -> None:
        rows = [{"role": "assistant", "content": f"x {SECRET}", "ts": "t", "meta": {"s": {1}}}]
        with pytest.raises(TypeError):
            _expected(rows)
        with pytest.raises(TypeError):
            _actual(rows)
        assert wire_render.render_memo_info()[0] == 0

    @pytest.mark.parametrize("role", [["user"], {"k": "v"}])
    def test_a_row_with_an_unhashable_role_renders_like_the_unmemoized_path(
        self, role: object
    ) -> None:
        rows = [*_rows(), {"role": role, "content": f"hand edited {SECRET}", "ts": "t9"}]
        expected = _expected(rows)
        assert _actual(rows) == expected
        assert _actual(rows) == expected

    def test_an_edited_row_is_rendered_again(self) -> None:
        rows = _rows()
        _actual(rows)
        rows[1]["content"] = f"now {SECRET} differs"
        rows[1]["meta"]["tool"]["title"] = "changed nested value"
        assert _actual(rows) == _expected(rows)


class TestEveryOutsideInputIsInTheKey:
    def test_exempt_host_change_re_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hosts: dict[str, frozenset[str]] = {"v": frozenset()}
        monkeypatch.setattr(cu, "_exempt_exact_hosts", lambda: hosts["v"])
        monkeypatch.setattr(exfil, "_exempt_exact_hosts", lambda: hosts["v"])
        rows = _rows()
        first = _actual(rows)
        assert "tenant.example.com/collect?data=" not in first
        hosts["v"] = frozenset({"tenant.example.com"})
        second = _actual(rows)
        assert second == _expected(rows)
        assert "tenant.example.com/collect?data=" in second

    def test_workspace_allowed_hosts_are_in_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def allowed(workspace: str | None) -> frozenset[str]:
            return frozenset({"tenant.example.com"}) if workspace == "/ws/a" else frozenset()

        monkeypatch.setattr(cu, "allowed_hosts_for", allowed)
        rows = _rows()
        plain = _actual(rows, workspace="/ws/b")
        relaxed = _actual(rows, workspace="/ws/a")
        assert plain == _expected(rows, workspace="/ws/b")
        assert relaxed == _expected(rows, workspace="/ws/a")
        assert plain != relaxed

    def test_token_prefix_change_re_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        prefixes: dict[str, tuple[str, ...]] = {"v": ()}
        monkeypatch.setattr(cu, "_token_param_exempt_prefixes", lambda: prefixes["v"])
        rows = _rows()
        _actual(rows)
        before = wire_render.render_memo_info()[0]
        prefixes["v"] = ("tok_",)
        _actual(rows)
        assert wire_render.render_memo_info()[0] > before

    def test_live_child_change_re_renders_the_oauth_row(self) -> None:
        rows = _rows()
        alive = _actual(rows, live_child="c-1")
        dead = _actual(rows, live_child="c-2")
        assert alive == _expected(rows, live_child="c-1")
        assert dead == _expected(rows, live_child="c-2")
        assert '"expired": true' in dead and '"expired": true' not in alive


class TestOwnerView:
    def test_owner_view_neither_reads_nor_writes_the_memo(self) -> None:
        redacted_row = [{"role": "assistant", "content": f"k {SECRET}", "ts": "a"}]
        fresh_row = [{"role": "assistant", "content": f"other {SECRET}", "ts": "b"}]
        _actual(redacted_row)
        entries = wire_render.render_memo_info()[0]
        with owner_view():
            assert _actual(fresh_row) == _expected(fresh_row)
            assert SECRET in _actual(fresh_row)
            _actual(redacted_row)
        assert wire_render.render_memo_info()[0] == entries


class TestPageScope:
    SCOPE = ("session", None, False)

    def test_page_uses_the_memo_only_after_its_fill(self) -> None:
        assert wire_render.plan_read(self.SCOPE) == (False, True)
        # A read while the fill is pending neither uses the memo nor asks again.
        assert wire_render.plan_read(self.SCOPE) == (False, False)
        wire_render._settle_scope(self.SCOPE, True)
        assert wire_render.plan_read(self.SCOPE) == (True, False)
        assert wire_render.plan_read(("session", 200, False)) == (False, True)

    def test_a_fill_that_did_not_run_is_asked_for_again(self) -> None:
        wire_render.plan_read(self.SCOPE)
        wire_render._settle_scope(self.SCOPE, False)
        assert wire_render.plan_read(self.SCOPE) == (False, True)

    @pytest.mark.asyncio
    async def test_schedule_fill_fills_off_the_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wire_render, "FILL_DELAY_S", 0.0)
        rows = _rows()
        assert wire_render.plan_read(self.SCOPE) == (False, True)
        wire_render.schedule_fill(self.SCOPE, rows, False, live_child="c-1", workspace=None)
        assert wire_render.render_memo_info()[0] == 0
        await _drain_fills()
        entries = wire_render.render_memo_info()[0]
        assert entries > 0
        assert wire_render.plan_read(self.SCOPE) == (True, False)
        assert _actual(rows) == _expected(rows)
        assert wire_render.render_memo_info()[0] == entries

    @pytest.mark.asyncio
    async def test_fill_waits_for_its_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wire_render, "FILL_DELAY_S", 3600.0)
        wire_render.plan_read(self.SCOPE)
        wire_render.schedule_fill(self.SCOPE, _rows(), False, live_child="c-1", workspace=None)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not wire_render._fills
        assert wire_render.render_memo_info()[0] == 0
        assert wire_render.plan_read(self.SCOPE) == (False, False)

    @pytest.mark.asyncio
    async def test_fill_over_the_limit_leaves_the_page_unfilled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wire_render, "FILL_DELAY_S", 0.0)
        monkeypatch.setattr(wire_render, "_FILLS_MAX", 0)
        wire_render.plan_read(self.SCOPE)
        wire_render.schedule_fill(self.SCOPE, _rows(), False, live_child="c-1", workspace=None)
        await _drain_fills()
        assert wire_render.render_memo_info()[0] == 0
        assert wire_render.plan_read(self.SCOPE) == (False, True)


async def _drain_fills() -> None:
    """Let zero-delay fills start, then wait for every one to finish.

    Each pass yields to the loop: a finished task leaves ``_fills`` from its
    done callback, which runs only once the loop gets a turn.
    """
    for _ in range(5):
        await asyncio.sleep(0)
    while wire_render._fills:
        await asyncio.gather(*list(wire_render._fills))
        await asyncio.sleep(0)


class TestBounded:
    def test_accounted_bytes_stay_under_the_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wire_render, "RENDER_MEMO_MAX_BYTES", 4096)
        rows = [{"role": "assistant", "content": f"row {i} " + "x" * 200} for i in range(200)]
        assert _actual(rows) == _expected(rows)
        entries, accounted = wire_render.render_memo_info()
        assert 0 < entries < 200
        assert accounted <= 4096

    def test_entry_cap_holds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wire_render, "RENDER_MEMO_MAX_ENTRIES", 10)
        rows = [{"role": "assistant", "content": f"row {i}"} for i in range(50)]
        _actual(rows)
        assert wire_render.render_memo_info()[0] == 10


CHANNEL_KEY = "slack:1785370133.085469"
SLOT_NAME = "slack_1785370133.085469"


def _write_transcript(tmp_path: Any, count: int) -> None:
    path = tmp_path / f"{_safe_key(CHANNEL_KEY)}.jsonl"
    lines = [json.dumps({"_type": "metadata", "created_at": "2026-07-30T00:00:00"})]
    for i in range(count):
        lines.append(
            json.dumps(
                {
                    "role": "user" if i % 4 == 0 else "assistant",
                    "content": f"message {i} body {SECRET if i % 7 == 0 else 'plain'}",
                    "ts": f"2026-07-30T00:{i // 60 % 60:02d}:{i % 60:02d}.{i:06d}Z",
                    "meta": {"mid": f"m{i}", "tool": {"name": "t", "status": "ok"}},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestEndpointDoesNotRehashUnchangedRows:
    """A second read of an unchanged session digests no message string again."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["?limit=100", ""])
    async def test_second_read_skips_the_per_string_digest(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, query: str
    ) -> None:
        state = _make_state(tmp_path)
        state.push_slots_update = lambda: None  # type: ignore[method-assign]
        _write_transcript(tmp_path, 700)
        messages = state.conversation_log.read_messages_chained(CHANNEL_KEY)
        slot = channel_slots.surface_channel_session(
            state,
            {"key": _safe_key(CHANNEL_KEY), "title": "Bench"},
            state.conversation_log.get_metadata(CHANNEL_KEY),
            messages,
            session_key=CHANNEL_KEY,
        )
        assert slot is not None
        digests = {"n": 0}
        real = cu._display_redaction_cache_key

        def counted(*args: Any, **kwargs: Any) -> Any:
            digests["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(cu, "_display_redaction_cache_key", counted)
        monkeypatch.setattr(wire_render, "FILL_DELAY_S", 0.0)
        async with TestClient(TestServer(_make_app(state))) as client:
            bodies = []
            counts = []
            for _ in range(2):
                digests["n"] = 0
                resp = await client.get(f"/api/chat/slots/{SLOT_NAME}{query}")
                assert resp.status == 200
                bodies.append(await resp.json())
                counts.append(digests["n"])
                # The first read fills the memo in the background; wait for it.
                await _drain_fills()

        rows = len(bodies[0]["messages"])
        assert rows == (100 if query else 700)
        assert counts[0] >= rows
        # The title and render context are digested; no message string is.
        assert counts[1] <= 2
        assert bodies[0] == bodies[1]
        assert SECRET not in json.dumps([m for m in bodies[1]["messages"] if m["role"] != "user"])
