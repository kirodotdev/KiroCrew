"""Parity + regression tests for centralized script-hook validation.

Issue #5444: the skills-plus-command invariant, event membership, and timeout
bounds were enforced only in ``ScriptHookStore.update``, so a direct caller of
``ScriptHookStore.create`` could persist a hook the update path rejects and that
later silently fails to fire. Deserialization (``ScriptHook.from_dict``) also
accepted out-of-range timeouts and did not recognize inline regex flag-DISABLING
forms such as ``(?-i)``.

These tests prove all three entry paths (create, update, deserialization) now
share one contract, that malformed persisted hooks load fail-soft rather than
taking down the store, and that the inline-flag detector handles disabling and
scoped forms.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    HOOK_TIMEOUT_DEFAULT,
    HOOK_TIMEOUT_MAX,
    HOOK_TIMEOUT_MIN,
    ScriptHook,
    ScriptHookStore,
    _context_matches,
    _has_global_inline_flags,
    validate_hook_fields,
)
from kiro_crew.webhooks import WebhookStoreUnreadable


def _valid(**overrides) -> dict:
    """A minimal valid create payload, with field overrides applied."""
    base = {"name": "h", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "command": "true"}
    base.update(overrides)
    return base


# ── create/update parity: same definition accepted or rejected identically ──


class TestCreateUpdateParity:
    """A definition create rejects, update must reject too — and vice versa."""

    # command+skills invariant

    def test_create_rejects_command_plus_skills(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        with pytest.raises(ValueError, match="skills cannot be combined with a command"):
            store.create(_valid(command="true", skills=["deploy"]))

    def test_update_rejects_command_plus_skills(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid())
        with pytest.raises(ValueError, match="skills cannot be combined with a command"):
            store.update(hook.id, {"skills": ["deploy"]})

    def test_create_rejects_empty_hook(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        with pytest.raises(ValueError, match="either command or skills"):
            store.create({"name": "h", "event": HOOK_EVENT_USER_PROMPT_SUBMIT})

    def test_update_rejects_clearing_both_command_and_skills(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid())
        with pytest.raises(ValueError, match="either command or skills"):
            store.update(hook.id, {"command": ""})

    # skills-event pairing

    def test_create_rejects_skills_on_tool_event(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        with pytest.raises(ValueError, match="skills hooks cannot fire on"):
            store.create({"name": "h", "event": HOOK_EVENT_PRE_TOOL_USE, "skills": ["x"]})

    def test_update_rejects_skills_on_tool_event(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h", "event": HOOK_EVENT_PRE_TOOL_USE, "command": "true"})
        with pytest.raises(ValueError, match="skills hooks cannot fire on"):
            store.update(hook.id, {"command": "", "skills": ["x"]})

    def test_create_accepts_skills_only_on_agent_spawn(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h", "event": HOOK_EVENT_AGENT_SPAWN, "skills": ["deploy"]})
        assert hook.skills == ["deploy"]
        assert hook.command == ""

    # event membership

    def test_create_rejects_invalid_event(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        with pytest.raises(ValueError, match="invalid event"):
            store.create(_valid(event="NotAnEvent"))

    def test_update_rejects_invalid_event(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid())
        with pytest.raises(ValueError, match="invalid event"):
            store.update(hook.id, {"event": "NotAnEvent"})

    # timeout bounds

    @pytest.mark.parametrize("bad", [0, -5, HOOK_TIMEOUT_MAX + 1, "30", 1.5, True])
    def test_create_rejects_out_of_range_timeout(self, tmp_path, bad):
        store = ScriptHookStore(tmp_path)
        with pytest.raises(ValueError, match="timeout must be an integer"):
            store.create(_valid(timeout=bad))

    @pytest.mark.parametrize("bad", [0, -5, HOOK_TIMEOUT_MAX + 1, "30", 1.5, True])
    def test_update_rejects_out_of_range_timeout(self, tmp_path, bad):
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid())
        with pytest.raises(ValueError, match="timeout must be an integer"):
            store.update(hook.id, {"timeout": bad})

    @pytest.mark.parametrize("good", [HOOK_TIMEOUT_MIN, HOOK_TIMEOUT_MAX, 30])
    def test_create_accepts_in_range_timeout(self, tmp_path, good):
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(timeout=good))
        assert hook.timeout == good

    # regex matcher syntax

    def test_create_rejects_invalid_regex(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        with pytest.raises(ValueError, match="invalid regex"):
            store.create(_valid(matcher="[invalid", matcher_mode="regex"))

    def test_update_rejects_invalid_regex(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid())
        with pytest.raises(ValueError, match="invalid regex"):
            store.update(hook.id, {"matcher": "[invalid", "matcher_mode": "regex"})


# ── validate_hook_fields: the single shared contract ──


class TestValidateHookFields:
    def test_valid_command_hook_passes(self):
        validate_hook_fields(
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            timeout=30,
            command="true",
            skills=[],
            matcher="",
            matcher_mode="glob",
        )

    def test_valid_skills_hook_passes(self):
        validate_hook_fields(
            event=HOOK_EVENT_AGENT_SPAWN,
            timeout=30,
            command="",
            skills=["deploy"],
            matcher="",
            matcher_mode="glob",
        )

    def test_boundary_timeouts_pass(self):
        for t in (HOOK_TIMEOUT_MIN, HOOK_TIMEOUT_MAX):
            validate_hook_fields(
                event=HOOK_EVENT_STOP,
                timeout=t,
                command="true",
                skills=[],
                matcher="",
                matcher_mode="glob",
            )


# ── deserialization: fail-soft, never abort the store ──


class TestFromDictNormalization:
    @pytest.mark.parametrize(
        "bad_timeout,expected",
        [
            (0, HOOK_TIMEOUT_MIN),  # clamp up
            (-5, HOOK_TIMEOUT_MIN),
            (99999, HOOK_TIMEOUT_MAX),  # clamp down
            ("nope", HOOK_TIMEOUT_DEFAULT),  # junk -> default
            (None, HOOK_TIMEOUT_DEFAULT),
            (True, HOOK_TIMEOUT_DEFAULT),  # bool is not a real timeout
        ],
    )
    def test_from_dict_clamps_timeout(self, bad_timeout, expected):
        hook = ScriptHook.from_dict({"name": "h", "command": "true", "timeout": bad_timeout})
        assert hook.timeout == expected

    def test_from_dict_never_raises_on_command_plus_skills(self):
        # Deserialization is fail-soft: a malformed persisted hook must LOAD
        # (visibly inert), not raise and abort the whole store. The write
        # boundary is where the invariant is enforced.
        hook = ScriptHook.from_dict(
            {"name": "h", "command": "true", "skills": ["x"], "event": HOOK_EVENT_STOP}
        )
        assert hook.command == "true"
        assert hook.skills == ["x"]


class TestStoreLoadResilience:
    def test_one_malformed_hook_does_not_drop_the_others(self, tmp_path):
        path = tmp_path / "hooks.json"
        path.write_text(
            json.dumps(
                {
                    "hooks": [
                        {"id": "good1", "name": "g1", "command": "true"},
                        "this is not a dict",  # malformed entry
                        {"id": "good2", "name": "g2", "command": "true"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = ScriptHookStore(tmp_path)
        ids = {h.id for h in store.list_all()}
        assert ids == {"good1", "good2"}

    @pytest.mark.parametrize("payload", [{"hooks": None}, ["not", "an", "object"]])
    def test_malformed_hooks_container_is_inert_until_mutation(self, tmp_path, payload):
        path = tmp_path / "hooks.json"
        original = json.dumps(payload)
        path.write_text(original, encoding="utf-8")

        store = ScriptHookStore(tmp_path)

        assert store.list_all() == []
        with pytest.raises(WebhookStoreUnreadable, match="refusing to overwrite"):
            store.create({"name": "new", "command": "true"})
        assert store.list_all() == []
        assert path.read_text(encoding="utf-8") == original

    def test_invalid_event_entry_is_inert_and_survives_mutation(self, tmp_path):
        invalid = {
            "id": "future-event",
            "name": "future event",
            "event": "FutureEvent",
            "command": "true",
        }
        path = tmp_path / "hooks.json"
        path.write_text(
            json.dumps(
                {
                    "hooks": [
                        invalid,
                        {"id": "good", "name": "good", "command": "true"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        store = ScriptHookStore(tmp_path)

        assert store.get("future-event") is None
        assert store.update("good", {"name": "updated"}) is not None
        saved = json.loads(path.read_text(encoding="utf-8"))["hooks"]
        assert invalid in saved
        assert any(
            hook.get("id") == "good" and hook.get("name") == "updated"
            for hook in saved
            if isinstance(hook, dict)
        )

    def test_unhashable_id_is_skipped_without_dropping_siblings(self, tmp_path):
        (tmp_path / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": [
                        {"id": [], "name": "bad", "command": "true"},
                        {"id": "good", "name": "good", "command": "true"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        store = ScriptHookStore(tmp_path)

        assert [hook.id for hook in store.list_all()] == ["good"]

    def test_unparseable_entry_survives_fire_persistence(self, tmp_path):
        malformed = {"id": [], "name": "backup", "command": "true"}
        path = tmp_path / "hooks.json"
        path.write_text(
            json.dumps(
                {
                    "hooks": [
                        malformed,
                        {
                            "id": "good",
                            "name": "good",
                            "event": HOOK_EVENT_STOP,
                            "command": "true",
                            "enabled": False,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = ScriptHookStore(tmp_path)

        asyncio.run(store.fire(HOOK_EVENT_STOP))

        saved = json.loads(path.read_text(encoding="utf-8"))["hooks"]
        assert malformed in saved
        assert any(hook.get("id") == "good" for hook in saved if isinstance(hook, dict))

    def test_out_of_range_persisted_timeout_loads_clamped(self, tmp_path):
        path = tmp_path / "hooks.json"
        path.write_text(
            json.dumps({"hooks": [{"id": "h", "name": "h", "command": "true", "timeout": 0}]}),
            encoding="utf-8",
        )
        store = ScriptHookStore(tmp_path)
        loaded = store.get("h")
        assert loaded is not None
        assert loaded.timeout == HOOK_TIMEOUT_MIN


# ── inline regex flag handling (issue: (?-i) and scoped flags) ──


class TestInlineFlagGroupDetection:
    @pytest.mark.parametrize(
        "pattern",
        [
            "(?i)foo",
            "(?im)foo",
            "(?aiLmsux)foo",
        ],
    )
    def test_recognizes_global_flag_groups(self, pattern):
        assert _has_global_inline_flags(pattern) is True

    @pytest.mark.parametrize(
        "pattern",
        [
            "foo",
            "(?i:foo)",  # scoped flags do not replace the outer default
            "(?i-s:foo)",
            "(?-i:foo)",
            "(?:foo)",
            "(?=foo)",
            "(?<=foo)bar",
            "(?P<name>foo)",
            "(?)foo",
        ],
    )
    def test_rejects_scoped_and_non_flag_groups(self, pattern):
        assert _has_global_inline_flags(pattern) is False


class TestRegexMatcherFlagHandling:
    def test_scoped_disabling_flag_is_honored_without_changing_suffix_default(self):
        # The matcher default remains case-insensitive outside the scoped group,
        # while (?-i:foo) makes only `foo` case-sensitive.
        assert _context_matches(r"(?-i:foo)bar", "regex", "fooBAR") is True
        assert _context_matches(r"(?-i:foo)bar", "regex", "FOObar") is False

        # A scoped set/unset group still controls DOTALL inside its own body.
        assert _context_matches(r"(?i-s:foo.bar)", "regex", "FOOxBAR") is True
        assert _context_matches(r"(?i-s:foo.bar)", "regex", "FOO\nBAR") is False

    def test_default_is_case_insensitive(self):
        # No inline flag group -> (?i) prepended -> case-insensitive default.
        assert _context_matches(r"foo", "regex", "FOO here")

    def test_leading_set_flag_group_still_matches(self):
        assert _context_matches(r"(?i)foo", "regex", "FOO")

    def test_scoped_set_flag_group_still_matches(self):
        assert _context_matches(r"(?i:foo)", "regex", "FOO")
