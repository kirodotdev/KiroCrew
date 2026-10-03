"""What ``GET /api/chat/slots/{slot}`` reports for a channel-born slot.

The refactor pointed a channel-born dashboard tab at the CHANNEL's own
transcript instead of a seeded copy, and capped the in-memory window at
``_RESTORE_WINDOW`` messages with ``_disk_older_count`` recording how many
on-disk lines the window omits.

The no-limit branch of the endpoint therefore reassembles
``disk[:older_count] + window`` and reports ``has_more=False``. These tests pin
down whether that claim is TRUE — i.e. whether the response really is the whole
conversation — for a transcript shorter than the window and for one longer than
it. If it is, no "load older messages" affordance is needed in the frontend;
``total`` is the real corpus size and there is nothing left on disk to fetch.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import channel_slots
from kiro_crew.history import _safe_key

CHANNEL_KEY = "slack:1785370133.085469"
SLOT_NAME = "slack_1785370133.085469"


def _write_transcript(tmp_path: Any, key: str, count: int) -> list[dict[str, Any]]:
    """Write *count* messages to *key*'s JSONL file and return them.

    Written directly rather than through ``ConversationLog.append`` so a
    700-message fixture does not pay 700 advisory-lock round trips.
    """
    path = tmp_path / f"{_safe_key(key)}.jsonl"
    msgs = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"m{i}",
            "ts": f"2026-07-30T00:00:{i % 60:02d}.{i:06d}Z",
        }
        for i in range(count)
    ]
    lines = [json.dumps({"_type": "metadata", "created_at": "2026-07-30T00:00:00"})]
    lines += [json.dumps(m) for m in msgs]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return msgs


def _surface(state: Any, key: str) -> Any:
    """Surface *key* as a dashboard slot exactly as the reconciler does."""
    messages = state.conversation_log.read_messages_chained(key)
    slot = channel_slots.surface_channel_session(
        state,
        {"key": _safe_key(key), "title": "Ship the thing"},
        state.conversation_log.get_metadata(key),
        messages,
        session_key=key,
    )
    assert slot is not None
    return slot


@pytest.fixture()
def state(tmp_path: Any) -> Any:
    st = _make_state(tmp_path)
    st.push_slots_update = lambda: None  # type: ignore[method-assign]
    return st


class TestNoLimitBranchReturnsTheWholeConversation:
    """``has_more=False`` is only honest if nothing is left behind on disk."""

    @pytest.mark.asyncio
    async def test_transcript_shorter_than_the_window_is_returned_whole(
        self, state: Any, tmp_path: Any
    ) -> None:
        """62 messages: the window holds them all, so no disk read is needed."""
        _write_transcript(tmp_path, CHANNEL_KEY, 62)
        slot = _surface(state, CHANNEL_KEY)
        assert slot._disk_older_count == 0
        assert len(slot.messages) == 62

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get(f"/api/chat/slots/{SLOT_NAME}")
            assert resp.status == 200
            body = await resp.json()

        assert len(body["messages"]) == 62
        assert body["total"] == 62
        assert body["has_more"] is False
        # Whole conversation, in order — first and last message both present.
        assert body["messages"][0]["content"] == "m0"
        assert body["messages"][-1]["content"] == "m61"

    @pytest.mark.asyncio
    async def test_transcript_longer_than_the_window_is_still_returned_whole(
        self, state: Any, tmp_path: Any
    ) -> None:
        """700 messages: 200 frozen-prefix lines are re-read and prepended.

        This is the case the plan assumed was truncated. It is not: the
        response carries all 700, so ``has_more=False`` is correct and
        ``total`` is the real corpus size.
        """
        _write_transcript(tmp_path, CHANNEL_KEY, 700)
        slot = _surface(state, CHANNEL_KEY)
        assert slot._disk_older_count == 700 - channel_slots._RESTORE_WINDOW
        assert len(slot.messages) == channel_slots._RESTORE_WINDOW

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get(f"/api/chat/slots/{SLOT_NAME}")
            assert resp.status == 200
            body = await resp.json()

        assert len(body["messages"]) == 700
        assert body["total"] == 700
        assert body["has_more"] is False
        assert body["messages"][0]["content"] == "m0"
        assert body["messages"][-1]["content"] == "m699"
        # No line is duplicated or dropped at the prefix/window seam.
        assert [m["content"] for m in body["messages"]] == [f"m{i}" for i in range(700)]

    @pytest.mark.asyncio
    async def test_exactly_the_window_length_needs_no_disk_read(
        self, state: Any, tmp_path: Any
    ) -> None:
        """Boundary: window-length transcript has an empty frozen prefix."""
        n = channel_slots._RESTORE_WINDOW
        _write_transcript(tmp_path, CHANNEL_KEY, n)
        slot = _surface(state, CHANNEL_KEY)
        assert slot._disk_older_count == 0

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get(f"/api/chat/slots/{SLOT_NAME}")).json()

        assert len(body["messages"]) == n
        assert body["total"] == n
        assert body["has_more"] is False

    @pytest.mark.asyncio
    async def test_one_past_the_window_recovers_the_single_older_line(
        self, state: Any, tmp_path: Any
    ) -> None:
        """Boundary: an off-by-one at the seam would drop or dupe ``m0``."""
        n = channel_slots._RESTORE_WINDOW + 1
        _write_transcript(tmp_path, CHANNEL_KEY, n)
        slot = _surface(state, CHANNEL_KEY)
        assert slot._disk_older_count == 1

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get(f"/api/chat/slots/{SLOT_NAME}")).json()

        assert [m["content"] for m in body["messages"]] == [f"m{i}" for i in range(n)]
        assert body["total"] == n
        assert body["has_more"] is False

    @pytest.mark.asyncio
    async def test_live_turns_appended_after_surfacing_are_included(
        self, state: Any, tmp_path: Any
    ) -> None:
        """The window keeps growing past ``_RESTORE_WINDOW`` in memory.

        ``_disk_older_count`` is frozen at surface time, so a slot that has
        since taken new turns must report prefix + the GROWN window, not a
        re-clamped 500.
        """
        _write_transcript(tmp_path, CHANNEL_KEY, 700)
        slot = _surface(state, CHANNEL_KEY)
        slot.append("user", "brand new", "msg msg-u", broadcast=False)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get(f"/api/chat/slots/{SLOT_NAME}")).json()

        assert len(body["messages"]) == 701
        assert body["total"] == 701
        assert body["has_more"] is False
        assert body["messages"][-1]["content"] == "brand new"
        assert body["messages"][0]["content"] == "m0"


class TestExplicitPaginationStillAgrees:
    """The legacy ``limit``/``before`` branch must span the same corpus.

    A frontend that asks for a page must not see a different conversation
    length than the one the no-limit open reported.
    """

    @pytest.mark.asyncio
    async def test_limited_page_reports_the_full_corpus_as_total(
        self, state: Any, tmp_path: Any
    ) -> None:
        _write_transcript(tmp_path, CHANNEL_KEY, 700)
        _surface(state, CHANNEL_KEY)

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get(f"/api/chat/slots/{SLOT_NAME}?limit=50")).json()

        assert len(body["messages"]) == 50
        assert body["total"] == 700
        # Genuine truncation on this branch DOES set has_more.
        assert body["has_more"] is True
        assert body["messages"][-1]["content"] == "m699"

    @pytest.mark.asyncio
    async def test_walking_before_backwards_reaches_the_first_message(
        self, state: Any, tmp_path: Any
    ) -> None:
        """``before`` is a chained-disk index; the walk must terminate at 0."""
        _write_transcript(tmp_path, CHANNEL_KEY, 700)
        _surface(state, CHANNEL_KEY)

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (
                await client.get(f"/api/chat/slots/{SLOT_NAME}?limit=100&before=100")
            ).json()

        assert [m["content"] for m in body["messages"]] == [f"m{i}" for i in range(100)]
        assert body["has_more"] is False
        assert body["total"] == 700


class TestTotalUnitsPerBranch:
    """``total`` counts in DIFFERENT units per branch, and the dashboard relies on it.

    The unbounded branch counts the raw transcript, every per-turn ``done`` row
    included; a bounded (``limit``) read counts after collapsing those rows away.
    The dashboard's shrink detection (``slotServerTotalRaw`` in
    ``website/src/store/chat/slotCache.ts``) records which kind of read a retained
    count came from and compares only like with like. Changing either branch's
    counting silently breaks that comparison, so the contract is pinned here,
    where the counting lives.
    """

    @pytest.mark.asyncio
    async def test_unbounded_counts_done_rows_and_bounded_does_not(
        self, state: Any, tmp_path: Any
    ) -> None:
        path = tmp_path / f"{_safe_key(CHANNEL_KEY)}.jsonl"
        rows: list[dict[str, Any]] = []
        for turn in range(5):
            rows.append(
                {
                    "role": "user",
                    "content": f"q{turn}",
                    "ts": f"2026-07-30T00:00:{turn:02d}.000001Z",
                }
            )
            rows.append(
                {
                    "role": "assistant",
                    "content": f"a{turn}",
                    "ts": f"2026-07-30T00:00:{turn:02d}.000002Z",
                }
            )
            rows.append(
                {"role": "done", "content": "", "ts": f"2026-07-30T00:00:{turn:02d}.000003Z"}
            )
        lines = [json.dumps({"_type": "metadata", "created_at": "2026-07-30T00:00:00"})]
        lines += [json.dumps(r) for r in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _surface(state, CHANNEL_KEY)

        async with TestClient(TestServer(_make_app(state))) as client:
            raw = await (await client.get(f"/api/chat/slots/{SLOT_NAME}")).json()
            bounded = await (await client.get(f"/api/chat/slots/{SLOT_NAME}?limit=100")).json()

        assert raw["total"] == 15
        assert bounded["total"] == 10


class TestRedactionGenerationOnTheRead:
    """A read reports the allow-list value its rows were served under.

    A tab seeds its baseline from its first read, so a change made between that
    read and its first status frame still counts as a change there. The value is
    the same ``list_fingerprint`` the status frame carries, on both branches.
    """

    @pytest.mark.asyncio
    async def test_both_branches_carry_the_list_value(
        self, state: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        monkeypatch.setattr(chat_handlers, "serving_generation", lambda: "gen-1")
        _write_transcript(tmp_path, CHANNEL_KEY, 5)
        _surface(state, CHANNEL_KEY)
        async with TestClient(TestServer(_make_app(state))) as client:
            raw = await (await client.get(f"/api/chat/slots/{SLOT_NAME}")).json()
            bounded = await (await client.get(f"/api/chat/slots/{SLOT_NAME}?limit=3")).json()
        assert raw["redaction_gen"] == "gen-1"
        assert bounded["redaction_gen"] == "gen-1"


class TestServingInputsArePinned:
    """A kept head (older rows the client holds above its newest page) is only
    re-served when ``serving_gen.serving_generation`` moves or the client marks
    its loaded rows changed. So every input that conditions what
    ``_prepare_messages`` serves must be registered in
    ``serving_gen.SERVING_INPUTS``, or the rows above the page go stale silently.
    This pins today's serving code: a new parameter, a new helper either body
    consults, or a new function reached anywhere below them (an existing helper
    starting to read a new setting) fails here, and whoever adds it decides
    whether it is an input before updating the pin. The walk follows functions
    named directly, through a module (``mod.helper()``, imported at module level
    or inside the function) and at any depth; it does not follow methods, so a
    serving input read through an object's method must be registered by review."""

    RULE = (
        "code reachable from _prepare_messages changed: if it reads any state that "
        "changes what is served, register it in serving_gen.SERVING_INPUTS (see the "
        "INVARIANT note on _prepare_messages); then update this pin"
    )

    def _inputs(self, fn: Any) -> tuple[list[str], list[str]]:
        import inspect

        from kiro_crew.dashboard import chat_utils

        params = list(inspect.signature(fn).parameters)
        helpers = sorted(set(fn.__code__.co_names) & set(vars(chat_utils)))
        return params, helpers

    def test_prepare_messages_inputs(self) -> None:
        from kiro_crew.dashboard.chat_utils import _prepare_messages

        assert self._inputs(_prepare_messages) == (
            ["messages", "running", "live_child", "workspace"],
            ["_prepare_messages_scoped", "allowed_hosts_for", "scoped_exempt_hosts"],
        ), self.RULE

    def test_prepare_messages_scoped_inputs(self) -> None:
        from kiro_crew.dashboard.chat_utils import _prepare_messages_scoped

        assert self._inputs(_prepare_messages_scoped) == (
            ["messages", "running", "live_child"],
            [
                "_collapse_wire_rows",
                "_expire_dead_child_oauth_meta",
                "_redact_meta_for_role",
                "_variant_for_emit",
                "parse_cls_meta",
                "redact_display_content",
                "serialize_wire_content",
                "with_allowed_links_restored",
            ],
        ), self.RULE

    #: Walks every project function ``_prepare_messages`` can call, transitively.
    #: Run in a FRESH interpreter: autouse test fixtures replace some helpers in
    #: the chain for the session, which would make the walk see test code.
    _REACH_SCRIPT = """
import json, sys, types
from kiro_crew.dashboard.chat_utils import _prepare_messages
def ours(obj):
    return (getattr(obj, "__name__", "") if isinstance(obj, types.ModuleType) else getattr(obj, "__module__", "") or "").startswith("kiro_crew")
seen = set()
refs = {}
stack = [_prepare_messages]
while stack:
    fn = stack.pop()
    name = (fn.__module__, fn.__qualname__)
    if name in seen:
        continue
    seen.add(name)
    read = set()
    codes = [fn.__code__]
    while codes:
        code = codes.pop()
        codes.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
        names = code.co_names
        # Only names that denote readable STATE: a module-level value that is not
        # a function or class (a setting, a cache, a context variable, a pattern)
        # or a module. Renaming or splitting a helper is not a new input, so it
        # does not move the fingerprint; reading a new setting does.
        g = fn.__globals__
        read.update(n for n in names if n in g and (isinstance(g[n], types.ModuleType) or not callable(g[n])))
        # Modules this code can reach a helper through: ``mod.helper()`` on a
        # module-level import, and function-local ``from x import mod`` / ``import x``.
        mods = [v for v in (fn.__globals__.get(n) for n in names) if isinstance(v, types.ModuleType) and ours(v)]
        for a in names:
            for b in (a, *(f"{a}.{n}" for n in names)):
                m = sys.modules.get(b)
                if m is not None and ours(m):
                    mods.append(m)
        for ref in names:
            targets = [fn.__globals__.get(ref)] + [getattr(m, ref, None) for m in mods]
            for target in targets:
                if isinstance(target, types.FunctionType) and ours(target):
                    stack.append(target)
    refs[f"{name[0]}.{name[1]}"] = sorted(read)
print(json.dumps({"seen": sorted(seen), "refs": refs}))
"""

    @classmethod
    def _reachable(cls) -> list[list[str]]:
        seen: list[list[str]] = cls._walk()["seen"]
        return seen

    _WALKED: dict[str, Any] | None = None

    @classmethod
    def _walk(cls) -> dict[str, Any]:
        if cls._WALKED is None:
            cls._WALKED = cls._run_walk()
        return cls._WALKED

    @classmethod
    def _run_walk(cls) -> dict[str, Any]:
        import os
        import subprocess
        import sys
        from pathlib import Path

        import kiro_crew

        src = str(Path(kiro_crew.__file__).resolve().parent.parent)
        path = os.pathsep.join(p for p in (src, os.environ.get("PYTHONPATH", "")) if p)
        out = subprocess.run(
            [sys.executable, "-c", cls._REACH_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONPATH": path},
            timeout=120,
            check=True,
        )
        return json.loads(out.stdout.strip().splitlines()[-1])

    #: Modules that HOLD or READ serving state -- the code rendering the rows, the
    #: allow list, the credential-pass switch, the exfil host exemptions, the OAuth
    #: endpoint config and the installed platform context. What they READ is pinned
    #: (``PINNED_READS``: the module-level state their reached functions name), not
    #: what their functions are called, so renaming or splitting a helper passes
    #: untouched and only a genuinely new state read fails and must be judged.
    _STATE_MODULES = frozenset(
        {
            "kiro_crew.config.loader",
            "kiro_crew.dashboard.chat_utils",
            "kiro_crew.dashboard.state",
            "kiro_crew.platform.context",
            "kiro_crew.security.exfil",
            "kiro_crew.security.redaction_allow",
            "kiro_crew.security.redaction_switch",
        }
    )

    @classmethod
    def _pinned_view(cls) -> list[str]:
        # Modules, not functions: a new module entering the chain is the event to
        # judge here; what a module reads is pinned separately below.
        return sorted({f"{module} (module)" for module, _ in cls._reachable()})

    def test_everything_prepare_messages_reaches(self) -> None:
        # Only a module NEW to the chain fails: one that drops out reads nothing.
        new = set(self._pinned_view()) - {
            "kiro_crew.atomic_write (module)",
            "kiro_crew.config.loader (module)",
            "kiro_crew.config.paths (module)",
            "kiro_crew.dashboard.chat_utils (module)",
            "kiro_crew.dashboard.state (module)",
            "kiro_crew.platform.context (module)",
            "kiro_crew.platform_compat (module)",
            "kiro_crew.platform_owner_compat (module)",
            "kiro_crew.security.exfil (module)",
            "kiro_crew.security.redaction (module)",
            "kiro_crew.security.redaction_allow (module)",
            "kiro_crew.security.redaction_switch (module)",
            "kiro_crew.windows_acl (module)",
        }
        assert not new, f"{self.RULE}\nnewly reached: {sorted(new)}"

    @classmethod
    def _pinned_reads(cls) -> dict[str, list[str]]:
        walk = cls._walk()
        reads: dict[str, set[str]] = {module: set() for module in cls._STATE_MODULES}
        for module, qualname in walk["seen"]:
            if module in reads:
                reads[module].update(walk["refs"][f"{module}.{qualname}"])
        return {module: sorted(names) for module, names in sorted(reads.items())}

    def test_every_state_read_in_a_serving_module_is_pinned(self) -> None:
        # The reach pin above sees a NEW module entering the chain, not an existing
        # function starting to read new state. In the modules that hold or read
        # serving state, the state the reached code reads (module-level values and
        # modules it names, nested code included; not the helpers it calls) is
        # pinned by name, so a helper there that begins to consult a new setting
        # fails here and must be judged as an input, while a renamed, split or
        # added helper that reads nothing new passes untouched. Only names absent
        # from the pin fail: a read that stops is no new input, so it costs no re-pin.
        new = {
            module: sorted(set(names) - set(PINNED_READS.get(module, ())))
            for module, names in self._pinned_reads().items()
        }
        new = {module: names for module, names in new.items() if names}
        assert not new, f"{self.RULE}\nnew reads: {new}"

    def test_the_generation_moves_with_every_registered_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import serving_gen

        assert [name for name, _ in serving_gen.SERVING_INPUTS] == [
            "allowed_hosts",
            "exempt_hosts",
            "oauth_endpoints",
        ]
        base = serving_gen.serving_generation()
        assert base == serving_gen.serving_generation()
        monkeypatch.setattr(serving_gen.redaction_allow, "list_fingerprint", lambda: "other-list")
        moved_list = serving_gen.serving_generation()
        assert moved_list != base
        monkeypatch.setattr(
            serving_gen.exfil, "_exfil_exempt_hosts", lambda: frozenset({"tenant.example"})
        )
        moved_exempt = serving_gen.serving_generation()
        assert moved_exempt != moved_list
        monkeypatch.setattr(
            serving_gen.exfil,
            "_load_operator_oauth_endpoints",
            lambda: frozenset({("login.example", "/authorize")}),
        )
        assert serving_gen.serving_generation() != moved_exempt

    def test_a_restarted_gateway_never_repeats_a_serving_generation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The list's write count restarts at zero with the process, so a list
        # revoked back to its boot state after a restart has the same inputs a
        # tab saw before it. The process's own value must still move the result.
        from kiro_crew.dashboard import serving_gen

        before = serving_gen.serving_generation()
        monkeypatch.setattr(serving_gen, "_BOOT", "another-process")
        assert serving_gen.serving_generation() != before

    def test_the_credential_switch_never_conditions_a_chat_render(self) -> None:
        # ``redaction_switch.credential_pass_bypassed`` is reachable from
        # ``_prepare_messages`` but True only inside ``owner_view()``, which only the
        # owner's file viewer enters. A chat render never does, so the switch is not
        # a serving input; this fails if that ever stops being true.
        import re
        from pathlib import Path

        import kiro_crew

        reached = {f"{module}.{qualname}" for module, qualname in self._reachable()}
        assert "kiro_crew.security.redaction_switch.owner_view" not in reached
        assert "kiro_crew.platform.context.redact_owner_view_via_context" not in reached
        root = Path(kiro_crew.__file__).resolve().parent
        entering = sorted(
            str(p.relative_to(root)).replace("\\", "/")
            for p in root.rglob("*.py")
            if re.search(
                r"(?<![`.\w])(owner_view|redact_owner_view_via_context)\(",
                p.read_text(encoding="utf-8"),
            )
            and p.name != "redaction_switch.py"
        )
        assert entering == ["dashboard/handlers/files.py", "platform/context.py"], self.RULE

    def test_the_published_generation_is_keyed_by_the_persisted_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hashlib
        import json as _json

        from kiro_crew.dashboard import serving_gen

        body = _json.dumps(
            [[name, read()] for name, read in serving_gen.SERVING_INPUTS], separators=(",", ":")
        ).encode("utf-8")
        published = serving_gen.serving_generation()
        # Not a plain digest of the inputs: a socket that can read the status frame
        # but not the owner-only allow-list cannot confirm a guessed list from it.
        assert published != hashlib.sha256(body).hexdigest()[:16]
        assert published != hashlib.sha256(serving_gen._DOMAIN + body).hexdigest()[:16]
        # The key is the persisted signing secret: the same secret (a restart)
        # answers the same value, another secret a different one.
        monkeypatch.setattr(serving_gen.token_secret, "_get_secret", lambda: b"k" * 32)
        assert serving_gen.serving_generation() == serving_gen.serving_generation() != published

    def test_an_oauth_file_edited_back_still_moves_the_generation(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from kiro_crew.config import loader as config_loader
        from kiro_crew.dashboard import serving_gen

        path = tmp_path / "oauth_endpoints.json"
        monkeypatch.setattr(config_loader, "oauth_endpoints_path", lambda: path)
        path.write_text("{}", encoding="utf-8")
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        first = serving_gen.serving_generation()
        path.write_text('{"x": 1}', encoding="utf-8")
        os.utime(path, ns=(2_000_000_000, 2_000_000_000))
        assert serving_gen.serving_generation() != first
        # Back to the first content: a render spanning both edits may hold rows
        # prepared under the middle one, so this must not read as the first state.
        path.write_text("{}", encoding="utf-8")
        os.utime(path, ns=(3_000_000_000, 3_000_000_000))
        assert serving_gen.serving_generation() != first

    def test_an_oauth_set_changed_and_restored_moves_the_generation_on_every_host(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config import loader as config_loader
        from kiro_crew.dashboard import serving_gen
        from kiro_crew.security import exfil

        # No stat to go on at all (on Windows a restore reproduces it): a render
        # that consumed the middle set still leaves its mark on the value.
        monkeypatch.setattr(config_loader, "oauth_endpoints_path", lambda: tmp_path / "absent.json")
        current: list[frozenset[tuple[str, str]]] = [frozenset()]
        monkeypatch.setattr(exfil, "_read_operator_oauth_endpoints", lambda: current[0])
        first = serving_gen.serving_generation()
        current[0] = frozenset({("idp.example", "/authorize")})
        exfil._load_operator_oauth_endpoints()  # a render prepares rows under it
        current[0] = frozenset()  # then the file is restored
        assert serving_gen.serving_generation() != first

    def test_an_oauth_backup_restored_with_its_old_stamp_still_moves_the_generation(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from kiro_crew.config import loader as config_loader
        from kiro_crew.dashboard import serving_gen

        # A backup copied back with its original mtime and the same size: only the
        # change time tells it apart, and a render spanning both copies may hold rows
        # prepared under the middle one. The stat is stood in for rather than read,
        # so this runs on every host, Windows included (whose kernel keeps the
        # creation time there; the change count covers that host's real restores).
        real = tmp_path / "oauth_endpoints.json"
        real.write_text("{}", encoding="utf-8")
        base = os.stat(real)
        change_time = [1_000_000_000]

        class _Restorable(type(real)):  # type: ignore[misc]
            def stat(self, **_: Any) -> os.stat_result:
                fields = list(base)
                fields[9] = change_time[0] // 1_000_000_000  # st_ctime (seconds)
                result = os.stat_result(fields, {"st_mtime_ns": 1, "st_ctime_ns": change_time[0]})
                return result

        monkeypatch.setattr(config_loader, "oauth_endpoints_path", lambda: _Restorable(real))
        first = serving_gen.serving_generation()
        change_time[0] = 2_000_000_000  # same mtime, size, device and inode
        assert serving_gen.serving_generation() != first


#: The module-level state each serving-state module's reached code reads, from
#: ``TestServingInputsArePinned._pinned_reads``. Regenerate it, and the reach list
#: above, with ``PYTHONPATH=src:test python test/test_slot_detail_full_history.py``
#: once a change there has been judged: a new serving input goes in
#: ``serving_gen.SERVING_INPUTS`` first.
PINNED_READS: dict[str, list[str]] = {
    "kiro_crew.config.loader": [],
    "kiro_crew.dashboard.chat_utils": [
        "REDACTION_RECORD_FIELDS",
        "_DISPLAY_REDACTION_CACHE_MAX_BYTES",
        "_DISPLAY_REDACTION_CACHE_MAX_ENTRIES",
        "_DISPLAY_REDACTION_SALT",
        "_display_redaction_cache",
        "_display_redaction_cache_bytes",
        "_display_redaction_cache_lock",
        "hashlib",
        "hmac",
        "json",
    ],
    "kiro_crew.dashboard.state": ["json"],
    "kiro_crew.platform.context": ["_ACTIVE"],
    "kiro_crew.security.exfil": [
        "ALLOWABLE_BLOCKED_LINK_RULES",
        "EXFILTRATION_REDACTION_TAG_PREFIX",
        "MAX_BLOCKED_LINKS_PER_MESSAGE",
        "MAX_BLOCKED_LINK_PATH_CHARS",
        "MAX_BLOCKED_LINK_QUERY_CHARS",
        "MAX_BLOCKED_LINK_URL_CHARS",
        "_BLOCKED_LINK_DOMAIN_RE",
        "_BLOCKED_LINK_RECORD_KEYS",
        "_BLOCKED_LINK_RULE_RE",
        "_BLOCKED_LINK_STRING_BOUNDS",
        "_BLOCKED_LINK_URL_WITHHELD",
        "_ENDPOINT_EXTENSION_CAP",
        "_ENDPOINT_EXTENSION_ENTRIES_KEY",
        "_EXFIL_PATTERNS",
        "_EXFIL_PERCENT_RE",
        "_EXFIL_QUERY_MIN_LEN",
        "_HARD_CREDENTIAL_RE",
        "_MAX_URL_DECODE_PASSES",
        "_OAUTH_AUTHORIZATION_ENDPOINTS",
        "_OAUTH_DIAGNOSTIC_PARAMETER_RE",
        "_OAUTH_ENTROPY_QUERY_PARAMS",
        "_OAUTH_EXTENSION_AUDITED",
        "_OAUTH_EXTENSION_HOST_RE",
        "_OAUTH_EXTENSION_MEMO",
        "_OAUTH_EXTENSION_PATH_BAD",
        "_OAUTH_EXTENSION_PATH_MAX_LEN",
        "_OAUTH_QUERY_PARAMS",
        "_OAUTH_S256_CHALLENGE_RE",
        "_OAUTH_URL_SYMBOLS",
        "_PLACEHOLDER_RE",
        "_S3_PRESIGNED_PARAMS",
        "_S3_PRESIGNED_RE",
        "_SCOPED_EXEMPT_HOSTS",
        "_SLACK_APP_CREATE_PARAMS",
        "_STRUCTURAL_VALIDATORS",
        "_URL_RE",
        "_oauth_extension_changes",
        "_oauth_extension_last",
        "_slack_manifest_re_slot",
        "json",
        "logger",
        "re",
        "string",
        "uuid",
    ],
    "kiro_crew.security.redaction_allow": [
        "DEFAULT_WORKSPACE",
        "MAX_HOSTS_PER_WORKSPACE",
        "MAX_WORKSPACES",
        "_HOST_RE",
        "_LOCK",
        "_WORKSPACE_RE",
        "_load_thread",
        "_loading",
        "_path_override",
        "_snapshot",
        "json",
        "threading",
    ],
    "kiro_crew.security.redaction_switch": ["_OWNER_VIEW_BYPASS"],
}


if __name__ == "__main__":
    # Print both pins in the shape they are written above, for a judged re-pin.
    print("_pinned_view() == [")
    for entry in TestServingInputsArePinned._pinned_view():
        print(f'    "{entry}",')
    print("]\nPINNED_READS = {")
    for module, names in TestServingInputsArePinned._pinned_reads().items():
        print(f'    "{module}": {names!r},'.replace("'", '"'))
    print("}")
