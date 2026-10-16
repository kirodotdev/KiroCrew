"""Shared fakes and builders for the Discord channel tests.

``test_discord.py`` and its ``test_discord_*.py`` siblings import from here, and
``test_discord.py`` re-exports the names other suites import from it. The fakes
stand in for the Discord REST client (``FakeClient``, with ``MultipartFake``'s
upload verbs), the provider and its event stream (``FakeProvider``, ``_Ev``), the
session manager (``FakeSessions``) and the turn context (``FakeCtx``).
``_dispatcher`` builds a dispatcher wired to them; ``_cfg``, ``_prime_live`` and
``_live_discord`` set the config the turn reads; ``_inbound`` builds a message.
``_drop_live_config_snapshot`` is autouse: every test module binds it, so no
primed config snapshot outlives the test that primed it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.discord.transport import DiscordInboundMessage
from kiro_crew.discord.transport_dispatch import DiscordDispatcher, _origin_kwargs, _QueuedOrigin
from kiro_crew.messaging.link import UNBIND_REASON_UNSPECIFIED
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session import _opt_out_key
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.session_map import ConversationOwnershipConflict

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# ── Fakes ──────────────────────────────────────────────────────────────────


class MultipartFake:
    """Share multipart verbs across Discord fake clients."""

    async def send_message_with_files(
        self,
        channel_id: str,
        text: str,
        files: Any,
        *,
        components: Any = None,
        reply_to_message_id: Any = None,
    ) -> str | None:
        if files:
            getattr(self, "uploads", []).append(("send", list(files)))
            if getattr(self, "raise_uploads", False):
                raise RuntimeError("multipart send exploded")
            if getattr(self, "fail_uploads", False):
                return None
        return await self.send_message(  # type: ignore[attr-defined]
            channel_id, text, components=components, reply_to_message_id=reply_to_message_id
        )

    async def send_document(
        self,
        channel_id: str,
        document: Any,
        *,
        caption: Any = None,
        reply_to_message_id: Any = None,
    ) -> str | None:
        """Record the destination alongside the file: the document verb routes a
        thread to its own channel id, which is the part a caller can get wrong."""
        getattr(self, "uploads", []).append(("document", [document]))
        getattr(self, "documents", []).append((channel_id, document, caption))
        if getattr(self, "raise_uploads", False):
            raise RuntimeError("document send exploded")
        if getattr(self, "fail_uploads", False):
            return None
        return await self.send_message(channel_id, caption or "")  # type: ignore[attr-defined]

    async def edit_message_with_files(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        files: Any,
        *,
        components: Any = None,
    ) -> bool:
        if files:
            getattr(self, "uploads", []).append(("edit", list(files)))
            if getattr(self, "raise_uploads", False):
                raise RuntimeError("multipart edit exploded")
            if getattr(self, "fail_uploads", False):
                return False
        return await self.edit_message(  # type: ignore[attr-defined]
            channel_id, message_id, text, components=components
        )

    async def edit_message_with_files_outcome(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        files: Any,
        *,
        components: Any = None,
    ) -> str:
        if getattr(self, "edit_gone", False):
            return "gone"
        if getattr(self, "fail_edits", False):
            return "failed"
        ok = await self.edit_message_with_files(
            channel_id, message_id, text, files, components=components
        )
        return "ok" if ok else "failed"


class FakeClient(MultipartFake):
    """Captures outbound Discord REST calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, str, Any]] = []
        #: channel_id per send_message / edit_message call (parallel to `sent` / `edits`).
        #: Which CHANNEL an outbound call addressed is otherwise invisible here, and it
        #: is the whole question for a queue shared by two people: a receipt edited
        #: under the wrong channel's address reaches a channel that message id does not
        #: exist in.
        self.send_channels: list[str] = []
        self.edit_channels: list[str] = []
        self.component_edits: list[tuple[str, Any]] = []
        self.acked: list[str] = []
        self.acked_destinations: list[str] = []
        self.dm_pairings: dict[str, str] = {}
        #: (interaction_id, text, ephemeral) per interaction callback response.
        self.responses: list[tuple[str, str, bool]] = []
        self.response_destinations: list[str] = []
        self.reactions: list[tuple[str, str]] = []
        self.thread_channels: set[str] = set()
        self.created_threads: list[tuple[str, str, str]] = []
        self.attachment_bodies: dict[str, bytes] = {}
        self.attachment_downloads: list[str] = []
        self.uploads: list[tuple[str, list[Any]]] = []
        #: (channel_id, document, caption) per name-preserving document send.
        self.documents: list[tuple[str, Any, Any]] = []
        self.edit_ok = True
        #: When set, every send returns None, which is what the real client does
        #: for a revoked token or a dead network.
        self.fail_sends = False
        self.fail_uploads = False
        self.raise_uploads = False
        self._mid = 100

    @property
    def uploaded_files(self) -> list[Any]:
        """Every attachment across all uploads, in order."""
        return [f for _verb, files in self.uploads for f in files]

    async def is_thread_channel(self, channel_id: str) -> bool:
        return channel_id in self.thread_channels

    async def send_typing(self, channel_id: str) -> None:
        return None

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: Any = None,
        reply_to_message_id: Any = None,
    ) -> str:
        # flake-ok: the fake client's zero-delay yield models a network await; moved verbatim
        await asyncio.sleep(0)  # yield like a real network await (exposes races)
        self._mid += 1
        self.sent.append((text, components))
        self.send_channels.append(channel_id)
        if self.fail_sends:
            return None
        return str(self._mid)

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        *,
        components: Any = None,
    ) -> bool:
        self.edits.append((message_id, text, components))
        self.edit_channels.append(channel_id)
        return self.edit_ok

    async def edit_message_components(
        self, channel_id: str, message_id: str, components: Any
    ) -> bool:
        self.component_edits.append((message_id, components))
        return True

    async def ack_component_interaction(
        self, interaction_id: str, interaction_token: str, *, destination: str = ""
    ) -> None:
        self.acked.append(interaction_id)
        self.acked_destinations.append(destination)

    async def respond_interaction(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        *,
        ephemeral: bool = True,
        components: Any = None,
        destination: str = "",
    ) -> bool:
        self.responses.append((interaction_id, text, ephemeral))
        self.response_destinations.append(destination)
        return True

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((message_id, emoji))

    def remember_dm_recipient(self, channel_id: str, user_id: str) -> None:
        self.dm_pairings[channel_id] = user_id

    async def create_dm_channel(self, user_id: str) -> str:
        return f"dm-{user_id}"

    async def create_thread_from_message(self, channel_id: str, message_id: str, name: str) -> str:
        thread_id = f"thread-{message_id}"
        self.created_threads.append((channel_id, message_id, name))
        self.thread_channels.add(thread_id)
        return thread_id

    async def download_attachment(self, url: str, dest: str) -> None:
        self.attachment_downloads.append(url)
        with open(dest, "wb") as fh:
            fh.write(self.attachment_bodies[url])

    def final_text(self) -> Any:
        """Text the user ultimately sees on the live message: the last edit if
        it was edited (edit-streaming), else the last send."""
        if self.edits:
            return self.edits[-1][1]
        return self.sent[-1][0] if self.sent else None

    def final_components(self) -> Any:
        if self.edits:
            return self.edits[-1][2]
        return self.sent[-1][1] if self.sent else None


class _Ev:
    def __init__(self, kind: str, text: str = "", stop_reason: str = "", title: str = "") -> None:
        self.kind = kind
        self.text = text
        self.stop_reason = stop_reason
        self.tool_call_id = ""
        self.title = title
        self.context_usage_pct = 0.0
        self.usage = None
        self.synthetic_completion = False


class FakeProvider:
    supports_steer, cwd = True, os.getcwd()

    def __init__(self, reply: str = "Answer") -> None:
        self._reply = reply
        self.steered: list = []
        self.cancelled = 0
        self.active_turn = True
        self.models: list[dict[str, str]] = []
        self.set_models: list[str] = []
        # ``!model`` reaches ``provider.client.set_model``, mirroring the real
        # AcpProvider's shape.
        self.client = SimpleNamespace(set_model=self._set_model)

    async def _set_model(self, model_id: str) -> None:
        self.set_models.append(model_id)

    def has_active_turn(self) -> bool:
        return self.active_turn

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> str:
        self.cancelled += 1
        return "acked"

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text=f"{self._reply}: {message[:16]}")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def stream_command(self, command: str) -> Any:
        yield _Ev(EVENT_COMPACTION_STATUS, text="completed", title="ok")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def compact(self, context: str = "") -> None:
        return None

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": "ok"}

    async def approve_tool(self, request_id: Any) -> None:
        return None

    async def reject_tool(self, request_id: Any) -> None:
        return None

    def available_models(self) -> list[dict[str, str]]:
        """What this session's backend advertised. Empty unless a test sets it,
        which is the real cold-start shape: nothing is advertised before a
        ``session/new``, and the picker must say so rather than offer an empty
        list."""
        return list(self.models)


class FakeSessions:
    async def aflush(self) -> None:  # in-memory double: already durable
        pass

    def __init__(self, raise_on_get: bool = False) -> None:
        self.released: list[str] = []
        self.acquired: list[str] = []
        self.destroyed: list[str] = []
        self.discarded: list[str] = []
        self.successes: list[str] = []
        self.failures: list[str] = []
        self.last_agent: Any = None
        self.last_model: Any = None
        self.last_provider: Any = None
        self.raise_on_get = raise_on_get
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0
        self._busy = False
        self._has = True
        self.queued: list = []
        self._gp = FakeProvider()
        self.mirror_links: dict[str, Any] = {}
        self.origin_links: dict[str, Any] = {}
        self.inbound_mirror_keys: set[str] = set()
        self.mirror_opt_outs: set[str] = set()
        #: Per key, what ``get_or_create`` captured as the superseded store.
        self.allocation_predecessors: dict[str, str] = {}
        #: Per key, the model ``get_or_create`` was asked for (the boundary's stamp).
        self.requested_models: dict[str, str] = {}
        # Batch bookkeeping, mirroring the real SessionManager: the unlink path
        # wraps its three clears in one batch, and a double without the context
        # manager would make that path unreachable from these tests. Each entry is
        # True when that mirror mutation ran inside a batch, so a test can pin
        # that one user-visible action costs one whole-map write.
        self.batch_depth = 0
        self.batched_writes: list[bool] = []
        # Interface parity with the real SessionManager: the dispatcher's
        # disconnect gate consults this. Entries are ``(session_key, origin)``.
        # Extended here rather than relying on the gate's fail-open, so a test
        # about the gate exercises the gate instead of its fallback.
        self.paused_deliveries: set[tuple[str, bool]] = set()

    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        return (key, origin) in self.paused_deliveries

    async def get_or_create(
        self,
        key: str,
        *,
        agent: Any = None,
        channel_id: Any = None,
        model: Any = None,
        wait_if_busy: bool = True,
        start_priority=None,
    ) -> Any:
        self.last_agent = agent
        self.last_model = model
        if self.raise_on_get:
            raise RuntimeError("cold-start failed")
        # The real boundary captures the store this allocation supersedes INSIDE
        # its registration's critical section (``SessionAllocationService
        # .allocation_predecessor``); the double mirrors that contract by reading
        # its own mapping stand-in at the moment it "allocates", when a test has
        # attached one.
        reader = getattr(self, "mapped_sid", None)
        if callable(reader):
            self.allocation_predecessors[key] = str(reader(key) or "")
        # The real boundary stamps the model the allocation SELECTED on the session
        # (``requested_model``); the double records the argument it was handed.
        self.requested_models[key] = str(model or "")
        # Recorded so a test can assert the dispatcher handed THIS provider on,
        # rather than merely handing on something.
        self.last_provider = FakeProvider()
        return self.last_provider, True, False

    def allocation_predecessor(self, key: str) -> str:
        return self.allocation_predecessors.get(key, "")

    def allocation_requested_model(self, key: str) -> str:
        return self.requested_models.get(key, "")

    def begin_turn(self, key: str) -> None:
        """The real manager's synchronous pre-dispatch closing gate."""
        self.begin_turns += 1
        if self.closing:
            raise SessionClosingError("SessionManager is closing")

    async def set_channel(self, key: str, channel: str) -> None:
        return None

    def record_success(self, key: str) -> None:
        self.successes.append(key)

    async def record_failure(self, key: str) -> None:
        self.failures.append(key)

    def check_context_usage(self, key: str, provider: Any) -> float:
        return 10.0

    def release(self, key: str) -> None:
        self.released.append(key)

    def get_provider(self, key: str) -> Any:
        return self._gp

    def is_busy(self, key: str) -> bool:
        return self._busy

    def max_generation(self, bucket: str) -> int:
        return -1

    def set_mirror_link(
        self,
        key: str,
        link: Any,
        *,
        accepts_inbound: bool = False,
        reason: str = UNBIND_REASON_UNSPECIFIED,
    ) -> None:
        # Interface parity with the real SessionMap: a conversation is exclusive
        # once it is inbound-committed — this claim is inbound-capable, or an
        # occupant already is. Two outbound-only mirrors stay allowed. A fake that
        # accepts what production refuses lets a test go green against a state the
        # product cannot reach.
        rivals = [
            other for other, held in self.mirror_links.items() if other != key and held == link
        ]
        if rivals and (
            accepts_inbound or any(other in self.inbound_mirror_keys for other in rivals)
        ):
            raise ConversationOwnershipConflict(
                f"{getattr(link, 'channel_type', '?')} conversation is already held"
            )
        self.batched_writes.append(self.batch_depth > 0)
        self.mirror_links[key] = link
        if accepts_inbound:
            self.inbound_mirror_keys.add(key)
        else:
            self.inbound_mirror_keys.discard(key)

    @contextmanager
    def batched_save(self) -> Any:
        self.batch_depth += 1
        try:
            yield
        finally:
            self.batch_depth -= 1

    def set_mirror_opt_out(self, key: str, opted_out: bool) -> None:
        # Bucket-keyed, like the real manager: the refusal is a preference about
        # the CONVERSATION, so it must outlive a generation rotation.
        self.batched_writes.append(self.batch_depth > 0)
        if opted_out:
            self.mirror_opt_outs.add(_opt_out_key(key))
        else:
            self.mirror_opt_outs.discard(_opt_out_key(key))

    def mirror_opt_out(self, key: str) -> bool:
        return _opt_out_key(key) in self.mirror_opt_outs

    def get_mirror_link(self, key: str) -> Any:
        return self.mirror_links.get(key)

    def set_origin_link(self, key: str, link: Any) -> None:
        self.origin_links[key] = link

    def get_origin_link(self, key: str) -> Any:
        return self.origin_links.get(key)

    def find_mirror_sessions(self, link: Any, *, inbound_only: bool = False) -> list[str]:
        return [
            key
            for key, candidate in self.mirror_links.items()
            if candidate == link and (not inbound_only or key in self.inbound_mirror_keys)
        ]

    def clear_mirror_link(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> bool:
        self.batched_writes.append(self.batch_depth > 0)
        self.inbound_mirror_keys.discard(key)
        self.batched_writes.append(self.batch_depth > 0)
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(
        self, link: Any, *, reason: str = UNBIND_REASON_UNSPECIFIED
    ) -> list[str]:
        self.batched_writes.append(self.batch_depth > 0)
        cleared = self.find_mirror_sessions(link)
        for key in cleared:
            self.inbound_mirror_keys.discard(key)
            self.mirror_links.pop(key, None)
        return cleared

    def enqueue(self, key: str, ts: str, text: str, *, force: bool = False, **kw: Any) -> bool:
        if force or self._busy:
            self.queued.append((ts, text, kw))
            return True
        return False

    def dequeue(self, key: str) -> Any:
        return self.queued.pop(0) if self.queued else None

    def clear_queue(self, key: str, owned_by: Any = None) -> None:
        self.queued.clear()

    def has_session(self, key: str) -> bool:
        return self._has

    async def try_acquire(self, key: str) -> bool:
        if self._busy or not self._has:
            return False
        self.acquired.append(key)
        return True

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)

    async def discard_conversation(self, key: str) -> None:
        self.discarded.append(key)

    def compact_wait_budget_secs(self) -> float:
        """The real manager's resolved ``session.compact_wait_secs`` (unset: 300 s)."""
        return 300.0


class _FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, *a: Any, **k: Any) -> Any:
        return SimpleNamespace(action="allow")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks = _FakeHooks()
        self.messages: list[str] = []

    def build_message(self, text: str, is_new: bool, key: str, **kw: Any) -> Any:
        self.messages.append(text)
        return text, None


def _cfg(soft: int = 80, default_agent: str = "", dm_scope: str = "per-channel-peer") -> Any:
    return SimpleNamespace(
        discord=SimpleNamespace(soft_threshold_pct=soft),
        agent=SimpleNamespace(default_agent=default_agent),
        messaging=SimpleNamespace(
            dm_scope=dm_scope,
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
        # Empty url is the shape a default install actually has.
        dashboard=SimpleNamespace(url=""),
    )


def _prime_live(cfg: Any) -> None:
    """Publish *cfg*'s ``discord`` and ``messaging`` fields as the live snapshot.

    The dispatcher reads those two sections at POINT OF USE from the config
    watcher rather than from the ``cfg=`` copy it was constructed with, so a
    test that varies one of them has to put the value where the turn actually
    looks for it. Every field the test's SimpleNamespace carries is copied onto
    a real ``KiroCrewConfig``, so the production readers see real sections and
    the loader's own defaults fill the rest.

    Call it again after mutating ``d.cfg`` mid-test -- the snapshot is a copy,
    not a view.
    """
    import dataclasses

    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    base = KiroCrewConfig()
    sections = {}
    for name in ("discord", "messaging"):
        section = getattr(cfg, name, None)
        if section is None:
            continue
        overrides = {
            f.name: getattr(section, f.name)
            for f in dataclasses.fields(getattr(base, name))
            if hasattr(section, f.name)
        }
        sections[name] = dataclasses.replace(getattr(base, name), **overrides)
    live.reset_for_tests()
    live.watch().prime(dataclasses.replace(base, **sections))


@pytest.fixture(autouse=True)
def _drop_live_config_snapshot():
    """Leave no primed config snapshot behind for the next test.

    ``_prime_live`` (and ``_dispatcher``, which calls it) publishes into the
    process-global config watcher, so without this the last test to prime would
    set the live config for every test after it in the same worker.
    """
    yield
    from kiro_crew.config import live

    live.reset_for_tests()


@contextlib.contextmanager
def _live_discord(**discord_kw: Any):
    """Put ``discord.*`` overrides in force for the body, then restore.

    For a field the dispatcher reads per TURN off the live snapshot rather than
    off its boot copy: the override has to be visible where the turn looks, and
    it has to be a real section so every other live read in the same turn still
    resolves.
    """
    import dataclasses

    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    previous = live.snapshot()
    base = previous if previous is not None else KiroCrewConfig()
    live.reset_for_tests()
    try:
        live.watch().prime(
            dataclasses.replace(base, discord=dataclasses.replace(base.discord, **discord_kw))
        )
        yield
    finally:
        live.reset_for_tests()
        if previous is not None:
            live.watch().prime(previous)


def _inbound_with_id(text: str, *, message_id: str, **kw: Any) -> InboundMessage:
    """An inbound message carrying Discord's raw message id, which is what the
    steer-ack reaction and the phase ladder both key on."""
    return DiscordInboundMessage(
        channel_type="discord",
        user_id=kw.pop("user_id", "u1"),
        conversation_id=kw.pop("conversation_id", "c1"),
        text=text,
        thread_id=kw.pop("thread_id", None),
        message_id=message_id,
    )


def _inbound(
    text: str,
    *,
    user_id: str = "u1",
    conversation_id: str = "c1",
    thread_id: str | None = None,
) -> InboundMessage:
    """A normalized Discord inbound message, as the transport would hand it over."""
    return InboundMessage(
        channel_type="discord",
        user_id=user_id,
        conversation_id=conversation_id,
        text=text,
        thread_id=thread_id,
    )


def _dispatcher(
    allowed: set[str],
    *,
    allowed_threads: set[str] | None = None,
    raise_on_get: bool = False,
    default_agent: str = "",
    dm_scope: str = "per-channel-peer",
) -> tuple[DiscordDispatcher, FakeClient, FakeSessions]:
    sess = FakeSessions(raise_on_get=raise_on_get)
    cfg = _cfg(default_agent=default_agent, dm_scope=dm_scope)
    _prime_live(cfg)
    d = DiscordDispatcher(
        sessions=sess,  # type: ignore[arg-type]
        ctx_builder=FakeCtx(),  # type: ignore[arg-type]
        cfg=cfg,
        allowed_user_ids=allowed,
        allowed_thread_ids=allowed_threads,
        agent=None,
        conv_log=None,
    )
    cli = FakeClient()
    d.client = cli  # type: ignore[assignment]
    return d, cli, sess


def _dc_origin(user: str = "u1", channel: str = "c1", *, thread: str = "") -> _QueuedOrigin:
    """One queued message's origin: who sent it, and where its reply goes.

    Defaults are user ``u1`` in channel ``c1``, the DM these tests use throughout.
    """
    return _QueuedOrigin(user_id=user, channel_id=channel, thread_id=thread)


def _origin(*a: Any, **kw: Any) -> dict[str, str]:
    """:func:`_dc_origin` as queue-entry kwargs, spelled by the PRODUCTION writer.

    A queue entry carries who sent it and where its reply goes, because the drain
    replays it under that envelope rather than under the turn that opened the queue.
    Built through ``_origin_kwargs`` rather than by spelling the storage keys, so
    renaming one moves this fixture with it instead of leaving it green against a
    shape production does not write.
    """
    return _origin_kwargs(_dc_origin(*a, **kw))
