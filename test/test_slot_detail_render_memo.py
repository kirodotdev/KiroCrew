"""The slot-detail render reuses unchanged rows without changing a byte of output.

``wire_render.prepare_messages_json`` must return exactly
``json.dumps(_prepare_messages(...))`` for every input, and must render a row
again whenever anything the display redaction reads has changed.
"""

from __future__ import annotations

import hashlib
import hmac
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


class TestContextMovedMidRender:
    def test_rows_rendered_while_the_context_moved_are_served_but_not_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _rows()
        readings = iter([b"before" * 3, b"after!" * 3])
        monkeypatch.setattr(wire_render, "_context_digest", lambda: next(readings))
        fragments, identities = wire_render.prepare_message_rows(rows, False, live_child="c-1")
        assert "[" + ", ".join(f for f in fragments if f is not None) + "]" == _expected(rows)
        assert wire_render.render_memo_info() == (0, 0)
        # No identity may vouch for a render whose context is not known.
        assert identities == [None] * len(identities)

    def test_a_steady_context_keeps_the_rows(self) -> None:
        rows = _rows()
        _, identities = wire_render.prepare_message_rows(rows, False, live_child="c-1")
        assert wire_render.render_memo_info()[0] > 0
        assert any(i is not None for i in identities)

    def test_an_exemption_dropped_mid_read_is_not_served_from_the_memo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fill the memo while the tenant host is exempt, so its entries hold the
        # tenant URL unredacted under that context's digest.
        hosts: dict[str, frozenset[str]] = {"v": frozenset({"tenant.example.com"})}
        monkeypatch.setattr(cu, "_exempt_exact_hosts", lambda: hosts["v"])
        monkeypatch.setattr(exfil, "_exempt_exact_hosts", lambda: hosts["v"])
        rows = _rows()
        real = wire_render._context_digest
        exempt_context = {"v": b""}

        def recording() -> bytes:
            exempt_context["v"] = real()
            return exempt_context["v"]

        monkeypatch.setattr(wire_render, "_context_digest", recording)
        relaxed, _ = wire_render.prepare_message_rows(rows, False, live_child="c-1")
        assert any(f and "tenant.example.com/collect?data=" in f for f in relaxed)
        # The exemption is withdrawn after the read took its first reading, so
        # every lookup still uses the exempt context's key.
        hosts["v"] = frozenset()
        readings = iter([exempt_context["v"]])
        monkeypatch.setattr(wire_render, "_context_digest", lambda: next(readings, None) or real())
        entries = wire_render.render_memo_info()
        fragments, identities = wire_render.prepare_message_rows(rows, False, live_child="c-1")
        served = "[" + ", ".join(f for f in fragments if f is not None) + "]"
        assert served == _expected(rows)
        assert "tenant.example.com/collect?data=" not in served
        assert identities == [None] * len(identities)
        assert wire_render.render_memo_info() == entries


def _legacy_display_key(text: str, base: Any) -> tuple[Any, int]:
    """The display key built the way it was before its suffix bytes were reused."""
    raw = text.encode("utf-8", errors="surrogatepass")
    hosts = "\0".join(sorted(cu._exempt_exact_hosts() | cu.current_scoped_exempt_hosts()))
    tail = hosts.encode("utf-8", errors="surrogatepass")
    prefixes = cu._token_param_exempt_prefixes()
    if prefixes:
        tail += b"\x01" + "\0".join(prefixes).encode("utf-8", errors="surrogatepass")
    if base is None:
        digest = hmac.new(cu._DISPLAY_REDACTION_SALT, raw + b"\0" + tail, hashlib.sha256).digest()
    else:
        mac = base.copy()
        mac.update(len(raw).to_bytes(8, "big"))
        mac.update(raw)
        mac.update(b"\0")
        mac.update(tail)
        digest = mac.digest()
    return (digest, len(raw)), len(raw)


class TestDisplayKeyIsUnchanged:
    """Reusing the key's suffix bytes changes no key, so stored verdicts still match."""

    @pytest.mark.parametrize("persistent", [False, True])
    def test_keys_match_the_previous_construction_as_the_sets_change(
        self, monkeypatch: pytest.MonkeyPatch, persistent: bool
    ) -> None:
        base = hmac.new(b"verdict-subkey", b"rules", hashlib.sha256) if persistent else None
        texts = ["", "plain", f"k {SECRET}", "caf\u00e9 \ud800 lone", "x" * 5000]
        exact_sets = [frozenset(), frozenset({"tenant.example.com"}), frozenset({"b.io", "a.io"})]
        prefix_sets: list[tuple[str, ...]] = [(), ("https://tenant.example.com/dl",)]
        seen = set()
        for exact in exact_sets:
            for prefixes in prefix_sets:
                for scoped in (frozenset(), frozenset({"a.io", "w.example"})):
                    monkeypatch.setattr(cu, "_exempt_exact_hosts", lambda e=exact: e)
                    monkeypatch.setattr(cu, "_token_param_exempt_prefixes", lambda p=prefixes: p)
                    with cu.scoped_exempt_hosts(scoped):
                        for text in texts:
                            new = cu._display_redaction_cache_key(text, base)
                            assert new == _legacy_display_key(text, base)
                            seen.add(new[0][0])
        # Every (sets, text) pair that differs in its MAC input got its own key.
        assert len(seen) > len(texts) * len(exact_sets)


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


class TestEndpointRowReuse:
    """Only the cursor read reuses rows; a plain read renders every row as before."""

    @staticmethod
    def _slot(state: Any, tmp_path: Any) -> Any:
        state.push_slots_update = lambda: None
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
        return slot

    @staticmethod
    def _counting(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        digests = {"n": 0}
        real = cu._display_redaction_cache_key

        def counted(*args: Any, **kwargs: Any) -> Any:
            digests["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(cu, "_display_redaction_cache_key", counted)
        return digests

    @pytest.mark.asyncio
    async def test_second_cursor_read_skips_the_per_string_digest(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _make_state(tmp_path)
        self._slot(state, tmp_path)
        digests = self._counting(monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            bodies = []
            counts = []
            for _ in range(2):
                digests["n"] = 0
                resp = await client.get(f"/api/chat/slots/{SLOT_NAME}?limit=100&cursor=1")
                assert resp.status == 200
                bodies.append(await resp.json())
                counts.append(digests["n"])

        assert len(bodies[0]["messages"]) == 100
        assert counts[0] >= 100
        # The title and render context are digested; no message string is.
        assert counts[1] <= 4
        assert bodies[0] == bodies[1]
        assert SECRET not in json.dumps([m for m in bodies[1]["messages"] if m["role"] != "user"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["?limit=100", ""])
    async def test_plain_reads_never_take_the_memo(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, query: str
    ) -> None:
        # A plain read has no row identities to pay for, so it renders with
        # _prepare_messages on every read and leaves the memo empty: there is
        # no page state that could keep pointing a read at evicted rows.
        state = _make_state(tmp_path)
        slot = self._slot(state, tmp_path)
        snapshots = {"n": 0}
        real_snapshot = wire_render._snapshot

        def counted_snapshot(m: dict) -> Any:
            snapshots["n"] += 1
            return real_snapshot(m)

        monkeypatch.setattr(wire_render, "_snapshot", counted_snapshot)
        async with TestClient(TestServer(_make_app(state))) as client:
            bodies = []
            for _ in range(3):
                resp = await client.get(f"/api/chat/slots/{SLOT_NAME}{query}")
                assert resp.status == 200
                bodies.append(await resp.text())
        assert snapshots["n"] == 0
        assert wire_render.render_memo_info() == (0, 0)
        assert bodies[0] == bodies[1] == bodies[2]
        rows = json.loads(bodies[0])["messages"]
        assert len(rows) == (100 if query else 700)
        if query:
            # The full read renders the slot's own copy of every row rather
            # than the stored transcript, so only the page read is compared.
            stored = state.conversation_log.read_messages_chained(CHANNEL_KEY)
            expected = cu._prepare_messages(
                stored[-len(rows) :], False, live_child="", workspace=slot.workspace
            )
            assert rows == json.loads(json.dumps(expected))
