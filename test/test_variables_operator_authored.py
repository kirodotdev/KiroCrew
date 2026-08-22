"""Only the operator's own dashboard text may have ``{{name}}`` expanded.

``_run_chat`` is the dashboard turn engine, but it is reached from ~22 call sites,
including the Slack linked-thread route, which hands it a channel participant's raw
message. While the expansion gate keyed only on ``is_slash`` and ``_prompt_depth``
-- both satisfied by an ordinary inbound message -- a participant could send
``{{NAME}}`` and read operator config back off the thread.

The transport ratchet in ``test_variables_channels.py`` could not catch this: the
expansion is not IN a transport module, it is in the dashboard engine the transport
calls. So this file guards the OTHER axis -- who is allowed to ask for expansion.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import kiro_crew.dashboard.chat_runner as runner_mod

SRC = pathlib.Path(runner_mod.__file__).resolve().parents[1]

# The only call sites permitted to pass operator_authored=True. Each is text the
# operator typed into the dashboard themselves. Adding an entry here is a security
# claim and should be argued in review, which is the point of pinning the set.
# The ONLY call site permitted to pass operator_authored=True: the composer POST,
# where the text arrives in an authenticated dashboard request body.
#
# The stored-message replay paths (regenerate, edit-resend, rewind) were briefly on
# this list and are deliberately NOT on it now. A stored user row is not proof the
# operator wrote it: slack/handler.py appends a channel participant's message as a
# user row on the linked-thread path, so replaying a stored row can replay
# participant text. Adding an entry here is a security claim about who can write the
# text that reaches it, and should be argued in review.
ALLOWED_OPT_IN = {
    "dashboard/chat_handlers.py",  # the composer POST
}


def _call_sites() -> list[tuple[str, int, bool]]:
    """Every ``_run_chat(...)`` call in the package, with whether it opts in."""
    found: list[tuple[str, int, bool]] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        # as_posix(), not str(): on Windows str() yields "dashboard\\chat_handlers.py",
        # which matches neither the allowlist nor the per-file checks below, so the
        # guard failed on the Windows shard while passing everywhere else.
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "_run_chat":
                continue
            # cli_chat defines an unrelated sync _run_chat(message, model, agent).
            if rel.startswith("cli_chat.py"):
                continue
            opts_in = any(
                kw.arg == "operator_authored"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            )
            found.append((rel, node.lineno, opts_in))
    return found


class TestExpansionIsOptIn:
    def test_the_parameter_defaults_to_false(self):
        """The default is the security property: a new caller does not expand."""
        param = inspect.signature(runner_mod._run_chat).parameters["operator_authored"]
        assert param.default is False, "expansion must be opt-in, never opt-out"
        assert (
            param.kind is inspect.Parameter.KEYWORD_ONLY
        ), "keyword-only so it can never be set by accident from position"

    def test_the_gate_requires_it(self):
        """Source guard: the expansion call must sit behind the new conjunct.

        Anchored on the conjunct rather than the whole expression, because the two
        older conjuncts have each been legitimately extended before.
        """
        src = inspect.getsource(runner_mod._run_chat)
        # Anchored on the identifier, not the call syntax: the site is now offloaded
        # via asyncio.to_thread, which passes the helper by name.
        gate = [ln for ln in src.splitlines() if "_expand_message_variables" in ln]
        assert len(gate) == 1, f"expected one expansion site, found {len(gate)}"
        assert (
            "if operator_authored and" in src
        ), "the expansion site is no longer gated on operator_authored"

    def test_only_allowlisted_call_sites_opt_in(self):
        """The enumeration. A new opt-in outside the allowlist fails here."""
        sites = _call_sites()
        assert sites, "found no _run_chat call sites; the walker is broken"
        offenders = sorted(
            {rel for rel, _lineno, opts_in in sites if opts_in and rel not in ALLOWED_OPT_IN}
        )
        assert not offenders, (
            "these call sites claim operator-authored text without being allowlisted: "
            f"{offenders}. If the claim is genuine, add it to ALLOWED_OPT_IN with a "
            "reason; if the text can come from a channel participant, it must not expand."
        )

    def test_the_slack_linked_thread_route_does_not_opt_in(self):
        """The specific regression. This caller passes a participant's raw text."""
        sites = _call_sites()
        slack_sites = [(rel, ln, opt) for rel, ln, opt in sites if rel == "slack/handler.py"]
        assert slack_sites, (
            "slack/handler.py no longer calls _run_chat; if the linked-thread route "
            "moved, re-point this test at its new home rather than deleting it"
        )
        for rel, lineno, opts_in in slack_sites:
            assert not opts_in, (
                f"{rel}:{lineno} opts into variable expansion, but the linked-thread "
                "route forwards a channel participant's message verbatim"
            )

    def test_stored_message_replay_paths_never_opt_in(self):
        """The vector the allowlist alone would not have caught.

        The first version of this fix DID opt these in, reasoning that a stored user
        row is the operator's own composer text. That reasoning was wrong:
        ``slack/handler.py`` appends a channel PARTICIPANT's message as a user row on
        the linked-thread path, so replaying a stored row can replay participant text
        and mirror the expansion back to the thread they read.

        Named per file rather than left to the allowlist, because the allowlist only
        says "these files may opt in" -- it would have happily accepted the wrong
        claim. This asserts the conclusion the claim was wrong about.
        """
        replay = {
            "dashboard/chat_regenerate.py": "regenerate and edit-resend",
            "dashboard/chat_rewind.py": "rewind replay",
        }
        offenders = [
            f"{rel}:{lineno} ({replay[rel]})"
            for rel, lineno, opts_in in _call_sites()
            if opts_in and rel in replay
        ]
        assert not offenders, (
            "a stored-message replay path opts into expansion: "
            f"{offenders}. A stored user row can hold channel-participant text."
        )

    def test_the_composer_does_opt_in(self):
        """Positive control. Without this the suite would pass on a feature that
        never expands anything at all -- which is exactly what an absence-only
        assertion cannot distinguish."""
        sites = _call_sites()
        composer = [
            (rel, ln) for rel, ln, opt in sites if rel == "dashboard/chat_handlers.py" and opt
        ]
        assert composer, (
            "the dashboard composer no longer opts in, so no operator text expands "
            "and the feature is inert"
        )


class TestStubsTrackTheRealSignature:
    """A hand-written ``_run_chat`` stand-in must tolerate extra keyword arguments.

    Adding ``operator_authored`` broke seven test stubs across two files, and the
    breakage surfaced as ``assert 500 == 200`` from the chat API rather than as
    anything mentioning the new parameter -- a TypeError inside the turn becomes a
    500. That cost a CI round, so the tolerance is pinned rather than left to be
    rediscovered the next time the signature grows.

    Scoped to stubs that stand in for the COMPOSER's call, since that is the only
    site passing the keyword; a stub patched over a channel path is unaffected and is
    deliberately not required to change.
    """

    def test_composer_path_stubs_accept_extra_kwargs(self):
        import ast

        offenders: list[str] = []
        for path in sorted((SRC.parent.parent / "test").glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            if "chat_handlers._run_chat" not in text:
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.args.kwarg is not None:
                    continue
                # Only STAND-INS, not helpers that merely mention run_chat in their
                # name. `_make_state_for_run_chat` builds a state object and is never
                # called with _run_chat's signature, so requiring **kwargs of it
                # would be noise that teaches the reader to ignore this guard.
                bare = node.name.lstrip("_")
                is_stub = bare.startswith(("fake", "stub", "mock")) and "run_chat" in bare
                if is_stub:
                    offenders.append(f"{path.name}:{node.lineno} {node.name}()")

        assert not offenders, (
            "these stubs stand in for the composer's _run_chat but reject extra "
            f"keyword arguments, so the next parameter added will 500: {offenders}"
        )
