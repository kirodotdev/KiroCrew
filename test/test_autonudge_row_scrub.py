"""Tests for the inbound conversation-row credential scrub and its per-row marker.

A persisted transcript row is an EGRESS: it is served back to every connected
dashboard client. Three claims are pinned here.

1. **Every user-row persister scrubs**, and the set of them does not grow
   unnoticed -- an AST ratchet walks the call sites rather than trusting a count.
2. **A rewritten row says so.** The scrub keeps no original, so an unmarked row
   would make a false positive indistinguishable from what the user typed.
3. **A miscomposed host withholds the row instead of raising.** Containment is
   the contract: half a turn on disk is worse than a withheld body.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import autonudge as _an


class TestEveryPersistedRowBodyUsesTheActiveCredentialPolicy:
    """First Principles (CONCERNS): the root cause was closed for user rows only.

    "The bare redactor pair skips a composed host's own patterns" was fixed at the user-row
    persisters, leaving the siblings that write the OTHER halves -- assistant/system rows at
    the history write boundary, the Slack nudge reply, and the cron run/result rows -- still
    on the bare pair. Model output carries credentials too, so a companion-only shape landed
    verbatim in the transcript. Every persisted row BODY now routes through the shim.
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

    def test_a_user_row_a_caller_rewrote_is_marked_too(self, tmp_path) -> None:
        """Design watch 1 / UX: the user's OWN words are the case that must carry the cue.

        The boundary exempts ``user``, so a comparison there sees no change: channel
        persisters scrub before calling in, and the row arrives already rewritten. The
        marker is therefore read off the stored text, which is the only evidence a
        reader has once no original is kept.
        """
        import json as _json

        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        # What a channel persister's own scrub produces, passed in as the user's row.
        log.append("slot-user", "user", "my key is [REDACTED: credential] ok")
        log.append("slot-user", "user", "just my ordinary question")
        rows = [
            _json.loads(line)
            for line in (tmp_path / "slot-user.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip() and _json.loads(line).get("role") == "user"
        ]
        assert len(rows) == 2, f"precondition: expected two user rows, got {len(rows)}"
        rewritten, verbatim = rows[0], rows[1]
        assert rewritten.get("redacted") is True, (
            "a user row rewritten by its persister carries no marker, so a redaction false "
            "positive silently replaces the user's own transcript text"
        )
        assert (
            "redacted" not in verbatim
        ), "a user row nobody rewrote was marked, so the cue would appear on verbatim text"

    def test_a_scrubbed_row_is_rewritten_while_a_clean_row_is_not(self, tmp_path) -> None:
        """Design (F2): the write-time scrub is a one-way door that has to say so.

        The nudge wire already carries ``message_redacted``; a conversation row that the
        scrub rewrote carried no equivalent, so a reader could not tell a redacted row from
        one the model wrote verbatim. Same contract, mirrored: a boolean set only on change.
        """
        import json as _json

        from kiro_crew.history import ConversationLog

        # Vendor-shaped so the HOST redactor masks it with no companion policy installed;
        # a vendor-less placeholder is not masked at all and would not exercise the flag.
        key = "AKIAIOSFODNN7EXAMPLE"
        log = ConversationLog(base_dir=tmp_path)
        log.append("slot-flagged", "assistant", f"deploy with {key}")
        log.append("slot-flagged", "assistant", "nothing sensitive here")
        rows = [
            _json.loads(line)
            for line in (tmp_path / "slot-flagged.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip() and _json.loads(line).get("role") == "assistant"
        ]
        assert len(rows) == 2, f"precondition: expected two assistant rows, got {len(rows)}"
        altered, untouched = rows[0], rows[1]
        assert (
            key not in altered["content"]
        ), "precondition: the scrub did not rewrite this row, so nothing was under test"
        assert untouched["content"] == "nothing sensitive here", (
            "a row with nothing credential-shaped in it was rewritten anyway, so the "
            "boundary is not shape-based"
        )
        assert altered.get("redacted") is True, (
            "the rewritten row carries NO marker, so a reader cannot tell it from what the "
            "author actually wrote and a false positive reads as the author's own words"
        )
        assert "redacted" not in untouched, (
            "an untouched row was marked as rewritten, so the marker says nothing and every "
            "row would carry the cue"
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

    ``_fire_slack_nudge`` scrubs the row it writes itself, and the dashboard nudge
    scrubs its own. The other channels do not write their own row: they hand a
    synthetic inbound to the dispatcher and the channel's ``_persist_turn`` writes
    it, so the store-sourced nudge text reached ``conv_log`` with nothing applied.

    Scrubbed at the SINK rather than at the door, because that is the only place the
    persisted copy is separable from the PROMPT. The prompt has already been
    consumed by the turn when ``_persist_turn`` runs, so redacting here cannot
    rewrite the instruction the model received -- the property the nudge depends on.

    Parametrized over all three channels that own a persister: a fix applied to only
    the two a reviewer happened to name leaves the third carrying the same hole.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _dispatcher_class(channel: str):
        if channel == "discord":
            from kiro_crew.discord.transport_dispatch import DiscordDispatcher

            return DiscordDispatcher
        if channel == "webex":
            from kiro_crew.webex.transport_dispatch import WebexDispatcher

            return WebexDispatcher
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        return TelegramDispatcher

    @pytest.mark.parametrize("channel", ["discord", "webex", "telegram"])
    def test_the_persisted_user_row_carries_no_credential(self, channel: str) -> None:
        """Fail-first: the row written to ``conv_log`` must not echo the stored secret."""
        cls = self._dispatcher_class(channel)
        rows: list[tuple[str, str]] = []

        def _append(_key, role, text, **_kw):
            rows.append((role, text))

        fake_log = MagicMock()
        fake_log.append = _append
        fake_log.append_if_absent = _append
        holder = SimpleNamespace(conv_log=fake_log)

        raw = f"[auto-nudge cycle 1]\ndeploy with {self.SECRET}"
        assert self.SECRET in raw, "fixture failed: the secret is not in the input"
        cls._persist_turn(holder, "chat-1-123", raw, "", False)

        user_rows = [text for role, text in rows if role == "user"]
        assert user_rows, f"fixture failed: {channel} persisted no user row to read"
        assert self.SECRET not in user_rows[0], (
            f"{channel} persisted the raw store-sourced turn: {user_rows[0]!r}. That row "
            "is served to dashboard readers, so a credential the ingress scan never saw "
            "reaches them verbatim"
        )

    @pytest.mark.parametrize("channel", ["discord", "webex", "telegram"])
    def test_a_clean_turn_is_persisted_unchanged(self, channel: str) -> None:
        """NEGATIVE CONTROL: the scrub must track CHANGE, not rewrite every row."""
        cls = self._dispatcher_class(channel)
        rows: list[tuple[str, str]] = []

        def _append(_key, role, text, **_kw):
            rows.append((role, text))

        fake_log = MagicMock()
        fake_log.append = _append
        fake_log.append_if_absent = _append
        holder = SimpleNamespace(conv_log=fake_log)

        clean = "[auto-nudge cycle 2]\njust keep going"
        cls._persist_turn(holder, "chat-1-123", clean, "", False)

        user_rows = [text for role, text in rows if role == "user"]
        assert user_rows, f"fixture failed: {channel} persisted no user row to read"
        assert user_rows[0] == clean, (
            f"{channel} altered a turn with nothing credential-shaped in it: "
            f"{user_rows[0]!r} != {clean!r}"
        )


# Every module that persists a transcript row with role "user". Pinned rather than
# derived so a NEW persister has to be added here deliberately.
_USER_ROW_PERSISTERS = frozenset(
    {
        "discord/transport_dispatch.py",
        "eval/runner.py",
        "feishu/transport_dispatch.py",
        "imessage/transport_dispatch.py",
        "llm_helpers.py",
        "slack/transport_dispatch.py",
        "taskrunner.py",
        "teams/transport_dispatch.py",
        "telegram/transport_dispatch.py",
        "webex/transport_dispatch.py",
        "wecom/transport_dispatch.py",
        "weixin/transport_dispatch.py",
        "whatsapp/transport_dispatch.py",
    }
)


def _appends_a_user_row(call: ast.Call) -> bool:
    """Does this call persist a ``ConversationLog`` row whose role is ``user``?

    Two shapes reach the same method and BOTH must be seen. Directly, the role is the
    second positional argument (``conv_log.append(key, "user", text)``). Indirectly, the
    bound method is handed to ``asyncio.to_thread`` and every argument shifts right by
    one, which is how the Slack dispatcher persists. Matching only the direct shape read
    Slack as unscrubbed while it was covered.

    The dashboard's in-memory slot is deliberately NOT matched: its ``append`` takes the
    role FIRST, and it is a different surface that stores what the user typed verbatim.
    """
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "append":
        args = call.args
        return len(args) >= 2 and isinstance(args[1], ast.Constant) and args[1].value == "user"
    if isinstance(func, ast.Attribute) and func.attr == "to_thread":
        args = call.args
        return (
            len(args) >= 3
            and isinstance(args[0], ast.Attribute)
            and args[0].attr == "append"
            and isinstance(args[2], ast.Constant)
            and args[2].value == "user"
        )
    return False


def _modules_persisting_user_rows() -> set[str]:
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    found: set[str] = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _appends_a_user_row(node):
                found.add(path.relative_to(root).as_posix())
    return found


def _enclosing_scopes(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Map every node to the nearest enclosing function, or the module."""
    owner: dict[ast.AST, ast.AST] = {}
    stack: list[ast.AST] = [tree]

    def walk(node: ast.AST) -> None:
        scope = stack[-1]
        for child in ast.iter_child_nodes(node):
            owner[child] = scope
            opens = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if opens:
                stack.append(child)
            walk(child)
            if opens:
                stack.pop()

    walk(tree)
    return owner


_SCRUBBERS = {"redact_via_context", "redact_row_via_context"}


def _scrubs_within(scope: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id in _SCRUBBERS for n in ast.walk(scope))


def _unscrubbed_user_row_sites() -> list[str]:
    """Every role=user append whose own function never calls the redactor.

    Scoped to the ENCLOSING FUNCTION rather than the file: the channel dispatchers
    rebind ``user_text = redact_via_context(user_text)`` above the call instead of
    wrapping the argument, so an argument-shaped check reports them all unscrubbed,
    while a whole-file check passes a second unscrubbed append in a module whose
    other function does scrub.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        owner = _enclosing_scopes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _appends_a_user_row(node)):
                continue
            scope = owner.get(node, tree)
            if not _scrubs_within(scope):
                name = getattr(scope, "name", "<module>")
                offenders.append(f"{path.relative_to(root).as_posix()}:{name}")
    return offenders


class TestEveryUserRowPersisterScrubs:
    """Ratchet: the transcript scrub is a convention across ~13 call sites, so pin them.

    Design and First-Principles review both landed on the same gap: the rule lives in
    each caller rather than at the write boundary, which is the weaker shape. Moving it
    into ``ConversationLog.append`` is AVAILABLE, not impossible -- it is declined here
    at a named cost, and the cost is the reason, not the impossibility. Those stored rows
    are read back into model context by the recall path, so scrubbing user
    rows there changes the prompt on resume; and on a host whose credential policy cannot
    compose, every dashboard user row would degrade to the withheld placeholder, which is
    then what the model resumes on. Until that trade is decided, enforcement has to be a
    guard that fails when the set moves, rather than a convention the next author has to
    remember.
    """

    def test_the_set_of_user_row_persisters_has_not_grown(self) -> None:
        """A fourteenth persister goes RED here instead of silently reopening the leak."""
        found = _modules_persisting_user_rows()
        assert found, "the scanner matched nothing, so it cannot detect a new persister"
        added = found - _USER_ROW_PERSISTERS
        assert not added, (
            "a module now persists a role=user transcript row without being registered "
            f"as scrubbing it: {sorted(added)}. Scrub the persisted copy (NOT the text "
            "handed to the model) and add the module to _USER_ROW_PERSISTERS."
        )
        assert not _USER_ROW_PERSISTERS - found, (
            "a registered persister no longer appends a user row; drop it from "
            f"_USER_ROW_PERSISTERS: {sorted(_USER_ROW_PERSISTERS - found)}"
        )

    def test_every_user_row_call_site_scrubs(self) -> None:
        """A new unscrubbed append goes red even in a module already registered."""
        offenders = _unscrubbed_user_row_sites()
        assert not offenders, (
            "these functions persist a role=user transcript row without calling "
            f"redact_via_context anywhere in the same function: {offenders}. Scrub the "
            "persisted copy, NOT the text handed to the model."
        )


class TestPersistedRowRedactionSurvivesAMiscomposedHost:
    """A composed-host failure must not kill ordinary inbound chat.

    ``redact_via_context`` re-raises ``PlatformCompositionError`` by design, which is
    right for a nudge or an API response: the caller answers it with an audited refusal.
    The channel persisters are different -- they sit on the dispatch path of ordinary
    conversation, so the same raise takes the message down with an unhandled exception
    on a host whose only fault is a companion it could not compose. Raised by the Design
    review. The containment must not become a downgrade: the raw text must never reach
    the transcript, which is the assertion that fails if the shim ever returns its input.
    """

    class _BrokenPolicy:
        def redact(self, text: str) -> str:
            from kiro_crew.platform import PlatformCompositionError

            raise PlatformCompositionError("companion credential policy unreadable")

    def _install(self, monkeypatch) -> None:
        from kiro_crew.platform import context as ctx_mod

        class _Ctx:
            credentials = TestPersistedRowRedactionSurvivesAMiscomposedHost._BrokenPolicy()

        # A context IS installed on such a host -- only its policy fails to compose. Stubbing
        # current_context alone left installed_context() None, the OTHER no-companion state.
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
            redact_row_via_context,
        )

        self._install(monkeypatch)
        secret = "token AKIAIOSFODNN7EXAMPLE"
        out = redact_row_via_context(secret)
        assert out == LOG_WITHHELD_PLACEHOLDER
        assert secret not in out

    def test_no_raising_redactor_feeds_the_replay_persister(self) -> None:
        """A turn that already went out must be persisted, so its scrub cannot raise."""
        path = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "slack" / "gateway.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders: list[str] = []
        reached = 0
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
        for path in sorted(root.rglob("*/transport_dispatch.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                if "redact_row_via_context" in names:
                    reached += 1
                if "redact_via_context" in names:
                    offenders.append(f"{path.parent.name}:{fn.name}")
        assert reached >= 9, f"probe reached only {reached} persisters -- it proves nothing"
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
        from kiro_crew.platform.context import redact_row_via_context

        rewritten = [text for text in self.CORPUS if redact_row_via_context(text) != text]
        assert not rewritten, (
            "the row scrub rewrote ordinary user prose, and the original is not retained, "
            f"so this destroys transcript text: {rewritten!r}"
        )

    def test_the_guard_can_detect_a_rewrite(self) -> None:
        """Positive control: a real credential MUST still be rewritten, or the corpus proves
        nothing."""
        from kiro_crew.platform.context import redact_row_via_context

        secret = "my key is AKIAIOSFODNN7EXAMPLE"
        assert redact_row_via_context(secret) != secret


def _slot_user_row_args(module: str) -> list[tuple[int, str]]:
    """Every ``slot.append("user", X, ...)`` in *module*, as ``(line, what X is)``.

    The role-FIRST shape, which the sibling scanner skips because a row typed into the
    dashboard is stored as typed. A row copied in from a channel is a different thing:
    it is inbound text on its way to a reader, so it carries the same egress duty as the
    persisters. ``X`` is reported as ``redacted`` only when it is a name this module bound
    from ``redact_row_via_context``.
    """
    root = Path(_an.__file__).resolve().parent
    tree = ast.parse((root / module).read_text(encoding="utf-8"))
    scrubbed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "redact_row_via_context"
        ):
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
                    "redacted" if arg.func.id == "redact_row_via_context" else arg.func.id,
                )
            )
        elif isinstance(arg, ast.Name):
            found.append((node.lineno, "redacted" if arg.id in scrubbed else f"raw:{arg.id}"))
        else:
            found.append((node.lineno, type(arg).__name__))
    return found


class TestChannelSourcedSlotRowsAreScrubbed:
    """First Principles: the sweep claimed EVERY inbound user message and missed two.

    The Slack mirror copied raw inbound text straight into the linked dashboard slot, and
    the recovery re-trigger scrubbed its row with the baseline pair instead of the host's
    composed policy. Both persist and both are served to dashboard readers, so a composed
    host's own patterns have to apply -- which is the whole claim this change makes.
    """

    def test_the_slack_mirror_scrubs_the_row_it_copies(self) -> None:
        rows = _slot_user_row_args("slack/handler.py")
        assert rows, "the scanner found no slot user row in the Slack handler, so it is blind"
        unscrubbed = [(line, what) for line, what in rows if what != "redacted"]
        assert not unscrubbed, (
            "the Slack mirror copies inbound text into the dashboard slot without the "
            f"composed-policy scrub: {unscrubbed!r}"
        )

    def test_the_recovery_retrigger_scrubs_with_the_composed_policy(self) -> None:
        rows = _slot_user_row_args("slack/gateway.py")
        assert rows, "the scanner found no slot user row in the gateway, so it is blind"
        unscrubbed = [(line, what) for line, what in rows if what != "redacted"]
        assert not unscrubbed, (
            "a gateway slot user row is persisted without the composed-policy scrub, so a "
            f"composed host's own patterns are skipped: {unscrubbed!r}"
        )
