"""Tests for the inbound conversation-row credential scrub and its per-row marker.

A persisted transcript row is an EGRESS: it is served back to every connected dashboard
client. Two claims are pinned here.

1. **The write boundary scrubs every role**, so a new persister is covered without
   being registered.
2. **A miscomposed host withholds the row instead of raising** -- half a turn on disk is
   worse than a withheld body.
"""

from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.history import _redact_at_write_boundary


class TestEveryPersistedRowBodyUsesTheActiveCredentialPolicy:
    """First Principles (CONCERNS): the root cause was closed for user rows only.

    "The bare redactor pair skips a composed host's own patterns" was fixed at the user-row
    persisters, leaving the OTHER halves -- assistant/system rows at the write boundary, the
    Slack nudge reply, the cron rows -- on the bare pair, and model output carries them too.
    """

    TOKEN = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

    class _CompanionPolicy:
        token = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

        def redact(self, text: str) -> str:
            from kiro_crew import security

            return security.redact(text).replace(self.token, "[REDACTED: companion]")

    @staticmethod
    def _install(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=policy))

    def test_the_bare_pair_alone_cannot_see_the_companion_shape(self) -> None:
        """POSITIVE CONTROL: without the shim the token survives, so the test can fail."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        bare, _ = redact_exfiltration_urls(f"key={self.TOKEN}")
        bare, _ = redact_credentials(bare)
        assert self.TOKEN in bare, (
            "precondition: the bare pair already redacts this shape, so neither arm of this "
            "test could distinguish the composed host from the baseline"
        )

    def test_a_non_user_history_row_is_scrubbed_by_the_composed_policy(self) -> None:
        from kiro_crew.history import _redact_at_write_boundary

        self._install(self._CompanionPolicy())
        try:
            stored = _redact_at_write_boundary("assistant", f"here it is: {self.TOKEN}")
        finally:
            from kiro_crew.platform import set_context

            set_context(None)
        assert self.TOKEN not in stored, (
            "an assistant row kept a companion-only credential verbatim, so the host's own "
            "patterns are still skipped for every non-user role"
        )
        assert "[REDACTED: companion]" in stored


class TestEveryChannelPersisterScrubsTheStoredTurn:
    """The persisted transcript row is an egress on every channel, not just Slack.

    Only ``_fire_slack_nudge`` and the dashboard nudge scrub the row they write; the others
    hand a synthetic inbound to the dispatcher and ``_persist_turn`` writes it, so
    store-sourced nudge text reached ``conv_log`` with nothing applied. The fake log applies
    ``_redact_at_write_boundary`` exactly as ``ConversationLog.append`` does.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _dispatcher_class(channel: str):
        module = import_module(f"kiro_crew.{channel}.transport_dispatch")
        return getattr(module, f"{channel.capitalize()}Dispatcher")

    @classmethod
    def _user_rows(cls, channel: str, turn: str) -> list[str]:
        rows: list[tuple[str, str]] = []

        def _append(_key, role, text, **_kw):
            rows.append((role, _redact_at_write_boundary(role, text)))

        fake_log = MagicMock()
        fake_log.append = _append
        fake_log.append_if_absent = _append
        holder = SimpleNamespace(conv_log=fake_log)
        cls._dispatcher_class(channel)._persist_turn(holder, "chat-1-123", turn, "", False)
        user_rows = [text for role, text in rows if role == "user"]
        assert user_rows, f"fixture failed: {channel} persisted no user row to read"
        return user_rows

    @pytest.mark.parametrize("channel", ["webex"])
    def test_the_persisted_user_row_carries_no_credential(self, channel: str) -> None:
        """Fail-first: the row written to ``conv_log`` must not echo the stored secret."""
        raw = f"[auto-nudge cycle 1]\ndeploy with {self.SECRET}"
        assert self.SECRET in raw, "fixture failed: the secret is not in the input"
        user_rows = self._user_rows(channel, raw)
        assert self.SECRET not in user_rows[0], (
            f"{channel} persisted the raw store-sourced turn: {user_rows[0]!r}. That row "
            "is served to dashboard readers, so a credential the ingress scan never saw "
            "reaches them verbatim"
        )

    @pytest.mark.parametrize("channel", ["discord", "webex", "telegram"])
    def test_a_clean_turn_is_persisted_unchanged(self, channel: str) -> None:
        """NEGATIVE CONTROL: the scrub must track CHANGE, not rewrite every row."""
        clean = "[auto-nudge cycle 2]\njust keep going"
        user_rows = self._user_rows(channel, clean)
        assert user_rows[0] == clean, (
            f"{channel} altered a turn with nothing credential-shaped in it: "
            f"{user_rows[0]!r} != {clean!r}"
        )


#: Modules whose OWN redactor call feeds a copy that bypasses the write boundary.
_TITLE_SCRUBBERS = tuple(
    f"{channel}/transport_dispatch.py"
    for channel in ("feishu", "imessage", "teams", "webex", "wecom", "weixin", "whatsapp")
)


class TestTheWriteBoundaryScrubsAUserRow:
    """First Principles: one boundary rule instead of a scrub repeated in every persister."""

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def test_a_user_row_is_scrubbed_at_rest(self, tmp_path) -> None:
        from kiro_crew.history import ConversationLog

        log = ConversationLog(tmp_path)
        log.append("slack:C1:1.0", "user", f"my key is {self.SECRET}")
        stored = log.read_messages("slack:C1:1.0")[0]["content"]
        assert self.SECRET not in stored, f"a user row was stored verbatim: {stored!r}"

    def test_a_clean_user_row_is_stored_unchanged(self, tmp_path) -> None:
        from kiro_crew.history import ConversationLog

        log = ConversationLog(tmp_path)
        log.append("slack:C1:1.0", "user", "rebase onto main please")
        assert log.read_messages("slack:C1:1.0")[0]["content"] == "rebase onto main please"


class TestPersistedRowRedactionSurvivesAMiscomposedHost:
    """A composed-host failure must not kill ordinary inbound chat.

    ``redact_via_context`` re-raises ``PlatformCompositionError`` by design, right for a nudge
    or an API response the caller answers with an audited refusal. The channel persisters sit on
    ordinary conversation's dispatch path, so that raise takes the message down on a host whose
    only fault is a companion it could not compose (Design review). Containment must not become
    a downgrade: the raw text must never reach the transcript.
    """

    class _BrokenPolicy:
        def redact(self, text: str) -> str:
            from kiro_crew.platform import PlatformCompositionError

            raise PlatformCompositionError("companion credential policy unreadable")

    def _install(self, monkeypatch) -> None:
        from kiro_crew.platform import context as ctx_mod

        class _Ctx:
            credentials = TestPersistedRowRedactionSurvivesAMiscomposedHost._BrokenPolicy()

        # A context IS installed here -- only its policy fails to compose.
        monkeypatch.setattr(ctx_mod, "installed_context", lambda: _Ctx())
        monkeypatch.setattr(ctx_mod, "current_context", lambda: _Ctx())

    def test_the_raising_spelling_still_raises_on_such_a_host(self, monkeypatch) -> None:
        from kiro_crew.platform import PlatformCompositionError
        from kiro_crew.platform.context import redact_via_context

        self._install(monkeypatch)
        with pytest.raises(PlatformCompositionError):
            redact_via_context("token AKIAIOSFODNN7EXAMPLE")

    def test_the_row_spelling_withholds_instead_of_raising(self, monkeypatch) -> None:
        from kiro_crew.platform.context import (
            LOG_WITHHELD_PLACEHOLDER,
            redact_log_via_context,
        )

        self._install(monkeypatch)
        secret = "token AKIAIOSFODNN7EXAMPLE"
        out = redact_log_via_context(secret)
        assert out == LOG_WITHHELD_PLACEHOLDER
        assert secret not in out

    def test_no_raising_redactor_feeds_the_replay_persister(self) -> None:
        """A turn that already went out must be persisted, so its scrub cannot raise."""
        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "slack"
        offenders: list[str] = []
        reached = 0
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                raised_names = {
                    target.id
                    for node in ast.walk(fn)
                    if isinstance(node, ast.Assign)
                    for target in node.targets
                    if isinstance(target, ast.Name)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "redact_via_context"
                }
                for call in ast.walk(fn):
                    if not isinstance(call, ast.Call):
                        continue
                    func = call.func
                    if not (
                        isinstance(func, ast.Name) and func.id == "save_conversation_turn_off_loop"
                    ):
                        continue
                    reached += 1
                    for arg in call.args:
                        if isinstance(arg, ast.Name) and arg.id in raised_names:
                            offenders.append(f"{fn.name}:{arg.id}")
                        elif (
                            isinstance(arg, ast.Call)
                            and isinstance(arg.func, ast.Name)
                            and arg.func.id == "redact_via_context"
                        ):
                            offenders.append(f"{fn.name}:inline")
        assert reached >= 2, f"probe found only {reached} persister call(s) -- it proves nothing"
        assert offenders == [], (
            "a completed turn is scrubbed by the RAISING spelling before being persisted, so a "
            "miscomposed host drops it from replay and restart history: " + "; ".join(offenders)
        )

    def test_every_channel_persister_uses_the_containment_spelling(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []
        reached = 0
        for rel in _TITLE_SCRUBBERS:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            scrubs = False
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                if "redact_log_via_context" in names:
                    scrubs = True
                if "redact_via_context" in names:
                    offenders.append(f"{rel}:{fn.name}")
            reached += scrubs
        assert reached == len(_TITLE_SCRUBBERS), (
            f"probe reached {reached} of {len(_TITLE_SCRUBBERS)} title scrubbers "
            "persisters -- it proves nothing"
        )
        assert offenders == [], (
            "a channel dispatch path still uses the raising spelling, so a miscomposed "
            "host kills the message instead of withholding the row: " + "; ".join(offenders)
        )


class TestOrdinaryUserProseSurvivesTheRowScrub:
    """Design's false-positive guard: the at-rest rewrite is irreversible, so it must not
    fire on ordinary prose."""

    CORPUS = (
        "can you rebase this branch onto main and push it",
        "the build failed with exit code 1, no other output",
        "my key insight is that the cache never invalidates",
        "set the token budget to 4096 and retry",
        "password rotation is due next quarter, per the runbook",
        "see https://github.com/acme/widgets/pull/42 for context",
        "AWS costs went up 12% after we moved to us-east-1",
        "the secret to this one is that ordering matters",
        "id 0123456789 belongs to the staging tenant",
        "Authorization is handled by the gateway, not here",
        "run `aws s3 ls` and paste the bucket names",
        "the access key rotation ticket is still open",
    )

    def test_no_ordinary_message_is_rewritten(self) -> None:
        from kiro_crew.platform.context import redact_log_via_context

        rewritten = [text for text in self.CORPUS if redact_log_via_context(text) != text]
        assert not rewritten, (
            "the row scrub rewrote ordinary user prose, and the original is not retained, "
            f"so this destroys transcript text: {rewritten!r}"
        )

    def test_the_guard_can_detect_a_rewrite(self) -> None:
        """Positive control: a real credential MUST still be rewritten."""
        from kiro_crew.platform.context import redact_log_via_context

        secret = "my key is AKIAIOSFODNN7EXAMPLE"
        assert redact_log_via_context(secret) != secret


def _binds_the_scrubber(value: ast.expr) -> bool:
    """Does this assigned value bind the row scrubber's result?

    Two legal spellings, because the scan is quadratic on uniform runs so a caller on the event
    loop hands it to a worker thread rather than calling it inline -- ``x =
    redact_log_via_context(text)`` and ``x = await asyncio.to_thread(redact_log_via_context,
    text)``. Recognising only the first inverts the ratchet: the SAFER spelling would go red.
    """
    inner = value.value if isinstance(value, ast.Await) else value
    if not isinstance(inner, ast.Call):
        return False
    if isinstance(inner.func, ast.Name) and inner.func.id == "redact_log_via_context":
        return True
    to_thread = (
        isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "to_thread"
        and bool(inner.args)
    )
    if not to_thread:
        return False
    first = inner.args[0]
    return isinstance(first, ast.Name) and first.id == "redact_log_via_context"


def _slot_user_row_args(module: str) -> list[tuple[int, str]]:
    """Every ``slot.append("user", X, ...)`` in *module*, as ``(line, what X is)``.

    The role-FIRST shape, which the sibling scanner skips because a row typed into the dashboard
    is stored as typed. A row copied in from a channel is inbound text on its way to a reader, so
    it carries the persisters' egress duty. ``X`` reads as ``redacted`` only when it is a name
    this module bound from ``redact_log_via_context``, inline or via a hop.
    """
    root = Path(_an.__file__).resolve().parent
    tree = ast.parse((root / module).read_text(encoding="utf-8"))
    scrubbed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if _binds_the_scrubber(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    scrubbed.add(target.id)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "append" or len(node.args) < 2:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "user"):
            continue
        arg = node.args[1]
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
            found.append(
                (
                    node.lineno,
                    "redacted" if arg.func.id == "redact_log_via_context" else arg.func.id,
                )
            )
        elif isinstance(arg, ast.Name):
            found.append((node.lineno, "redacted" if arg.id in scrubbed else f"raw:{arg.id}"))
        else:
            found.append((node.lineno, type(arg).__name__))
    return found


class TestChannelSourcedSlotRowsAreScrubbed:
    """First Principles: the sweep claimed EVERY inbound user message and missed two.

    The Slack mirror copied raw inbound text straight into the linked dashboard slot, and the
    recovery re-trigger scrubbed its row with the baseline pair, not the host's composed
    policy. Both persist and are served to readers, so a composed host's patterns must apply.
    """

    @pytest.mark.parametrize(
        "module",
        ["slack/handler_runtime/finalize.py", "slack/gateway_runtime/subagent_wiring.py"],
    )
    def test_a_channel_sourced_slot_row_is_scrubbed(self, module: str) -> None:
        rows = _slot_user_row_args(module)
        assert rows, f"the scanner found no slot user row in {module}, so it is blind"
        unscrubbed = [(line, what) for line, what in rows if what != "redacted"]
        assert not unscrubbed, (
            f"{module} copies inbound text into the dashboard slot without the "
            f"composed-policy scrub, so a composed host's own patterns are skipped: "
            f"{unscrubbed!r}"
        )


class TestTheScrubberBindingCheckStillRejectsRawText:
    """The ratchet accepts a second spelling, so prove it did not become unfalsifiable: a
    predicate that accepts everything would pass on a module that scrubs nothing.
    """

    @pytest.mark.parametrize(
        "src,binds",
        [
            ("x = redact_log_via_context(text)", True),
            ("async def f():\n    x = await asyncio.to_thread(redact_log_via_context, t)", True),
            ("x = text", False),
            ("x = some_other_helper(text)", False),
            ("x = text.strip()", False),
            # The hop shape with a DIFFERENT callee: the wrapper is not the credential.
            ("x = asyncio.to_thread(str.upper, text)", False),
            # A bare reference is not a binding -- naming the scrubber is not calling it.
            ("x = redact_log_via_context", False),
        ],
    )
    def test_only_a_real_binding_is_accepted(self, src: str, binds: bool) -> None:
        assign = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign))
        assert _binds_the_scrubber(assign.value) is binds
