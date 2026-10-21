"""Every production creator of a dashboard slot, classified against the follow-default rule.

Settings' "follow the default" press clears the model of every chat a person
started, and nothing undoes it. Which chats those are is decided by
``chat_follow_default._slot_model`` / ``_persons_line``: a chat counts unless it
shows a ``created_by``, an ``app``, a non-person origin, member mode, a channel
link or a remote executor. That rule fails open by design, because an origin of
``SlotOrigin.USER`` -- or ``""``, which ``get_or_create_slot`` gives a slot whose
caller declared none -- is also what non-human paths carry (the session-control
create verb mints USER slots on purpose). So a new automated slot kind that keeps
a person's origin and none of the other marks would have its model cleared by
the press.

This registry is the guard. It walks ``src/kiro_crew`` for every
``get_or_create_slot(...)`` call and every direct ``_ChatSlot(...)``
construction, charged to its innermost enclosing function, and pins one verdict
per site:

* ``person: ...`` -- a human request; counting it is correct.
* ``excluded: <field>=...`` -- the field the eligibility rule reads, and where the
  site sets it.
* ``no kept model: ...`` -- why the model this creator leaves is never one a
  person picked.

A new creator fails here until its author classifies it, and a creator that is
not a person's must set one of the excluded fields.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import kiro_crew

#: Directories under ``src/kiro_crew`` that hold tests or test helpers, not
#: production slot creators.
_SKIP_DIRS = frozenset({"tests", "container_tests", "testing"})

_CREATORS = frozenset({"get_or_create_slot", "_ChatSlot"})

_RESTORED = (
    "excluded: the persisted line's own origin, app, member mode and channel_origin "
    "(metadata_codec.slot_args), and _kept_models also reads that line, which keeps "
    "created_by"
)

#: (module relative to src/kiro_crew, innermost function) -> checked verdict.
SLOT_CREATORS: dict[tuple[str, str], str] = {
    ("apps/builtins/auto_research/campaign/agent_mode.py", "_launch_loop"): (
        "excluded: app=AUTO_RESEARCH_APP ('auto-research') at birth"
    ),
    ("apps/builtins/issue_radar/backend/crew_runtime_session.py", "ensure_crew_session"): (
        "excluded: app=APP_NAME ('issue-radar') at birth"
    ),
    ("apps/builtins/spec_builder/backend/runtime.py", "_ensure_worker_slot"): (
        "excluded: app=APP_NAME ('spec-builder') at birth"
    ),
    ("apps/worker_slots.py", "acquire_worker_slot"): (
        "excluded: app=app at birth, non-empty by _require_app(app)"
    ),
    ("dashboard/channel_slots.py", "surface_channel_session"): (
        "excluded: linked_session_key=session_key and channel_origin=True at birth"
    ),
    ("dashboard/chat_api/chat_send.py", "api_chat"): (
        "person: a dashboard send that opens its own tab; request_slot_origin is USER only "
        "with no app token and no attested cron creator (else APP with app=, or CRON)"
    ),
    ("dashboard/chat_api/resume.py", "_materialise_slot_from_history"): _RESTORED,
    ("dashboard/chat_api/slot_create.py", "api_chat_slot_create"): (
        "person: the New chat tab; request_slot_origin is USER only with no app token "
        "and no attested cron creator (else APP with app=, or CRON)"
    ),
    ("dashboard/chat_fork.py", "fork_slot"): (
        "person: a person's Fork (api_chat_fork, request_slot_origin); the session-control "
        "fork passes a stamp callback that sets _created_by on the child"
    ),
    ("dashboard/chat_persistence_parts/slot_builders.py", "_rehydrate_slot_from_history"): (
        _RESTORED
    ),
    ("dashboard/chat_persistence_parts/slot_builders.py", "_apply_recent_session"): _RESTORED,
    ("dashboard/cron_inject.py", "_bind_cron_slot"): "excluded: origin=SlotOrigin.CRON",
    ("dashboard/handlers/cron_runs.py", "api_cron_to_chat"): (
        "excluded: origin=SlotOrigin.CRON on both calls"
    ),
    ("dashboard/handlers/members_thread.py", "api_member_thread"): (
        "excluded: mode=members_mod.DM_SLOT_MODE ('member') at birth"
    ),
    ("dashboard/handlers/taskrunner.py", "_open_result_slot"): (
        "no kept model: neither call passes model=, and a turn writes slot.model only from a "
        "crewmate pin, which _on_pinned_crewmate leaves alone; the execution branch also "
        "passes linked_session_key= and sets _app"
    ),
    ("dashboard/handlers/taskrunner.py", "_task_result_slot"): (
        "no kept model: the gone-run fallback passes no model=, and a turn writes slot.model "
        "only from a crewmate pin, which _on_pinned_crewmate leaves alone"
    ),
    ("dashboard/openai_compat.py", "api_completions"): (
        "no kept model: the request's model field names an agent (slot.agent); slot.model is "
        "never written here, and a turn writes it only from a crewmate pin"
    ),
    ("dashboard/session_control_parts/session_create.py", "create_session"): (
        "excluded: created_by: slot._created_by = caller_key right after the mint, with no "
        "await between, and caller_key is refused when empty at entry"
    ),
    ("dashboard/state_parts/slot_directory.py", "get_or_create_slot"): (
        "no kept model: the constructor behind every entry here; it keeps the caller's model= "
        "and origin=, which are judged at that caller's own entry"
    ),
    ("dashboard/workflow_inject.py", "inject_workflow_result"): (
        "excluded: linked_session_key, assigned right after the mint with no await between"
    ),
    ("dashboard/workflow_inject.py", "inject_bound_workflow_result"): (
        "excluded: linked_session_key=origin at birth"
    ),
    ("slack/gateway_runtime/result_egress.py", "_deliver_result"): (
        "no kept model: a heartbeat delivery appends one assistant row and runs no turn, so "
        "slot.model stays '' until a person picks one"
    ),
    ("slack/interactions_options.py", "_import_thread_to_slot"): (
        "person: an allowed Slack user's !link-to-dashboard command or Link button importing "
        "their own thread; no model= is passed"
    ),
}

#: Sites holding more than one creator call; every other site holds exactly one.
_CALLS_PER_SITE: dict[tuple[str, str], int] = {
    ("dashboard/handlers/cron_runs.py", "api_cron_to_chat"): 2,
    ("dashboard/handlers/taskrunner.py", "_open_result_slot"): 2,
    ("dashboard/openai_compat.py", "api_completions"): 2,
}

_VERDICT_PREFIXES = ("person: ", "excluded: ", "no kept model: ")

#: Keywords of ``get_or_create_slot`` that the eligibility rule reads.
_EXCLUDING_KEYWORDS = frozenset({"origin", "app", "mode", "linked_session_key", "channel_origin"})


class _CreatorVisitor(ast.NodeVisitor):
    """Charges each slot creation to the innermost enclosing function."""

    def __init__(self, rel: str, found: dict[tuple[str, str], list[dict[str, str]]]) -> None:
        self.rel = rel
        self.found = found
        self.stack: list[str] = []

    def _func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name) else ""
        )
        if name in _CREATORS:
            keywords = {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}
            key = (self.rel, self.stack[-1] if self.stack else "")
            self.found.setdefault(key, []).append(keywords)
        self.generic_visit(node)


def _slot_creators() -> dict[tuple[str, str], list[dict[str, str]]]:
    """(module, innermost function) -> the keyword source text of each creator call."""
    root = Path(kiro_crew.__file__).parent
    found: dict[tuple[str, str], list[dict[str, str]]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if _SKIP_DIRS.intersection(rel.parts[:-1]):
            continue
        text = path.read_text(encoding="utf-8")
        if "get_or_create_slot" in text or "_ChatSlot" in text:
            _CreatorVisitor(rel.as_posix(), found).visit(ast.parse(text))
    return found


class TestSlotCreatorRegistry(unittest.TestCase):
    """A slot kind the follow-default press would wrongly clear cannot land unclassified."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.found = _slot_creators()

    def test_every_creator_is_classified(self):
        unclassified = sorted(set(self.found) - set(SLOT_CREATORS))
        self.assertEqual(
            unclassified,
            [],
            "a dashboard slot creator was added or moved: classify it in SLOT_CREATORS "
            "against chat_follow_default's eligibility rule (_slot_model / _persons_line). "
            "A slot a person did not start must set created_by, app, a non-USER origin, "
            "member mode or a channel link, or the Settings follow-default press clears "
            "its model, which cannot be undone",
        )
        counts = {key: len(calls) for key, calls in self.found.items()}
        expected = {key: _CALLS_PER_SITE.get(key, 1) for key in SLOT_CREATORS}
        self.assertEqual(
            counts,
            expected,
            "a creator call was added to or removed from a classified site: re-check its "
            "verdict in SLOT_CREATORS and its count in _CALLS_PER_SITE",
        )

    def test_no_stale_entry(self):
        stale = sorted(set(SLOT_CREATORS) - set(self.found))
        self.assertEqual(stale, [], "SLOT_CREATORS names a site that no longer creates a slot")
        self.assertLessEqual(set(_CALLS_PER_SITE), set(SLOT_CREATORS))

    def test_every_verdict_has_a_known_kind(self):
        for key, verdict in SLOT_CREATORS.items():
            with self.subTest(site=key):
                self.assertTrue(verdict.startswith(_VERDICT_PREFIXES), verdict)

    def test_an_excluded_keyword_is_really_passed(self):
        for key, verdict in SLOT_CREATORS.items():
            if not verdict.startswith("excluded: "):
                continue
            field = verdict.removeprefix("excluded: ").split("=", 1)[0]
            if field not in _EXCLUDING_KEYWORDS or "=" not in verdict:
                continue
            for keywords in self.found.get(key, []):
                with self.subTest(site=key, field=field):
                    self.assertIn(field, keywords, f"{key} does not pass {field}=")

    def test_an_excluding_origin_is_not_a_persons(self):
        checked = 0
        for key, verdict in SLOT_CREATORS.items():
            if not verdict.startswith("excluded: origin="):
                continue
            for keywords in self.found.get(key, []):
                with self.subTest(site=key):
                    origin = keywords.get("origin", "")
                    self.assertTrue(origin, f"{key} passes no origin=")
                    self.assertNotIn("USER", origin)
                    self.assertNotIn("request_slot_origin", origin)
                    self.assertNotIn(origin.strip("'\""), ("", "user"))
                    checked += 1
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
