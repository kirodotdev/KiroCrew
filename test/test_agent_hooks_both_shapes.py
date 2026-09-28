"""A spec's ``hooks`` field accepts Crew's object form and KAS's array form.

Four properties are pinned here:

* an array-form ``hooks`` value round-trips through the shared normalizer, with
  the fields kiro-cli cannot express still present in the stored spec;
* an object-form value serializes for kiro-cli byte for byte as it does without
  the array form existing, and does not go through the new normalizer at all;
* the same defect expressed in either shape is rejected by the same error path,
  with the same SEL reason;
* every guard the array form adds has a test that fails once that guard is gone.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.agent import (
    _MAX_HOOK_DESCRIPTION_LEN,
    _MAX_HOOK_NAME_LEN,
    _MAX_HOOK_PAYLOAD_LEN,
    _MAX_SPEC_HOOK_DOCUMENTS,
    _apply_user_kiro_hooks,
    hook_documents_suppressed_commands,
    hook_documents_to_object_form,
    normalize_spec_hooks,
)


def _hook_script(tmp_path: Path, name: str = "hook.sh") -> str:
    """Create an executable script and return its absolute path."""
    script = tmp_path / "hooks" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return str(script)


def _mc_cfg(kiro_hooks: object) -> dict:
    return {"agent": {"kiro_hooks": kiro_hooks, "kiro_hooks_autoimport": False}}


def _apply(kiro_hooks: object, *, bundled: dict | None = None) -> dict:
    """Run the merge over *kiro_hooks* and return the resulting hooks object."""
    config: dict = {"hooks": dict(bundled or {})}
    _apply_user_kiro_hooks(config, _mc_cfg(kiro_hooks))
    return config["hooks"]


@pytest.fixture()
def sel_calls(monkeypatch) -> list[tuple[str, str, str]]:
    """Capture every ``_sel_hook_rejected`` call as ``(event, command, reason)``."""
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        agent_mod,
        "_sel_hook_rejected",
        lambda event, command, reason: calls.append((event, command, reason)),
    )
    return calls


class TestArrayFormRoundTrip:
    """An array-form value survives load -> store -> load unchanged."""

    def _document(self, command: str) -> dict:
        return {
            "name": "guard",
            "description": "guards the shell",
            "trigger": "PreToolUse",
            "matcher": "execute_bash",
            "action": {"type": "command", "command": command},
            "timeout": 30,
            "enabled": True,
            "confirm": False,
        }

    def test_document_survives_normalize_store_normalize(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        first = normalize_spec_hooks([self._document(command)])
        # Store is the spec on disk: serialize the normalized form and read it
        # back through the same helper.
        stored = json.loads(json.dumps(first))
        second = normalize_spec_hooks(stored)
        assert first == second
        assert first == [self._document(command)]

    def test_normalizer_keeps_the_fields_kiro_cli_cannot_express(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        (doc,) = normalize_spec_hooks([self._document(command)])
        for field in ("name", "description", "timeout", "enabled", "confirm"):
            assert field in doc, f"{field} must stay in the stored spec"

    def test_agent_action_survives_normalization(self) -> None:
        (doc,) = normalize_spec_hooks(
            [
                {
                    "name": "ask",
                    "trigger": "Manual",
                    "action": {"type": "agent", "prompt": "summarize the diff"},
                }
            ]
        )
        assert doc["action"] == {"type": "agent", "prompt": "summarize the diff"}
        assert doc["trigger"] == "Manual"

    def test_array_form_reaches_the_generated_spec(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply([self._document(command)])
        assert hooks == {"preToolUse": [{"command": command, "matcher": "execute_bash"}]}

    @pytest.mark.parametrize(
        ("trigger", "event"),
        [
            ("PreToolUse", "preToolUse"),
            ("preToolUse", "preToolUse"),
            ("PostToolUse", "postToolUse"),
            ("postToolUse", "postToolUse"),
            ("UserPromptSubmit", "userPromptSubmit"),
            ("userPromptSubmit", "userPromptSubmit"),
            ("promptSubmit", "userPromptSubmit"),
            ("SessionStart", "agentSpawn"),
            ("sessionStart", "agentSpawn"),
            ("agentSpawn", "agentSpawn"),
            ("Stop", "stop"),
            ("agentStop", "stop"),
        ],
    )
    def test_expressible_triggers_map_to_kiro_cli_events(
        self, tmp_path: Path, trigger: str, event: str
    ) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply(
            [{"name": "h", "trigger": trigger, "action": {"type": "command", "command": command}}]
        )
        assert hooks == {event: [{"command": command}]}

    @pytest.mark.parametrize(
        "trigger",
        [
            "SessionEnd",
            "PreTaskExec",
            "preTaskExecution",
            "PostTaskExec",
            "postTaskExecution",
            "PostFileCreate",
            "fileCreated",
            "PostFileSave",
            "fileEdited",
            "AfterFileEdit",
            "PostFileDelete",
            "fileDeleted",
            "Manual",
            "userTriggered",
        ],
    )
    def test_triggers_kiro_cli_cannot_name_are_dropped_on_emission_only(
        self, tmp_path: Path, trigger: str
    ) -> None:
        command = _hook_script(tmp_path)
        document = {
            "name": "h",
            "trigger": trigger,
            "action": {"type": "command", "command": command},
        }
        assert normalize_spec_hooks([document]), "the document must stay in the stored spec"
        assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert _apply([document]) == {}

    def test_agent_action_is_dropped_on_emission_only(self) -> None:
        document = {
            "name": "ask",
            "trigger": "PreToolUse",
            "action": {"type": "agent", "prompt": "check the plan"},
        }
        assert normalize_spec_hooks([document])
        assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}

    def test_standalone_file_wrapper_is_not_a_spec_shape(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        wrapper = {
            "version": "v1",
            "hooks": [
                {
                    "name": "h",
                    "trigger": "PreToolUse",
                    "action": {"type": "command", "command": command},
                }
            ],
        }
        assert normalize_spec_hooks(wrapper) == []
        assert _apply(wrapper) == {}
        assert normalize_spec_hooks(wrapper.get("hooks")), "the inner array is a spec value"

    def test_a_value_that_is_not_an_array_is_ignored_with_an_audit(self, sel_calls) -> None:
        assert normalize_spec_hooks("preToolUse") == []
        assert [c for c in sel_calls if "not an array of hook documents" in c[2]]

    @pytest.mark.parametrize("value", ["preToolUse", 7, True])
    def test_the_merge_path_audits_a_value_of_neither_shape(self, sel_calls, value: object) -> None:
        """A string or number `kiro_hooks` is audited, not dropped in silence."""
        assert _apply(value) == {}
        assert [c for c in sel_calls if "not an array of hook documents" in c[2]]


class TestTheTriggerVocabularyMatchesKiroAgent:
    """Every spelling kiro-agent's alias table holds, and what it maps to.

    Transcribed from `packages/kiro-agent/src/hooks/trigger-names.ts` at blob
    `2d4a3127e32e5e81e68d5c2ea406a6a5728f6d78`. The table there is
    case-sensitive; Crew lowercases the key, so it accepts every spelling
    kiro-agent does plus casings of them.
    """

    ALIAS_TABLE = {
        "SessionStart": "SessionStart",
        "SessionEnd": "SessionEnd",
        "Stop": "Stop",
        "PreToolUse": "PreToolUse",
        "PostToolUse": "PostToolUse",
        "PreTaskExec": "PreTaskExec",
        "PostTaskExec": "PostTaskExec",
        "UserPromptSubmit": "UserPromptSubmit",
        "PostFileCreate": "PostFileCreate",
        "PostFileSave": "PostFileSave",
        "PostFileDelete": "PostFileDelete",
        "Manual": "Manual",
        "sessionStart": "SessionStart",
        "agentStop": "Stop",
        "promptSubmit": "UserPromptSubmit",
        "preTaskExecution": "PreTaskExec",
        "postTaskExecution": "PostTaskExec",
        "preToolUse": "PreToolUse",
        "postToolUse": "PostToolUse",
        "fileEdited": "PostFileSave",
        "fileCreated": "PostFileCreate",
        "fileDeleted": "PostFileDelete",
        "userTriggered": "Manual",
        "agentSpawn": "SessionStart",
        "stop": "Stop",
        "userPromptSubmit": "UserPromptSubmit",
        "AfterFileEdit": "PostFileSave",
    }

    def test_every_spelling_normalizes_to_its_canonical_trigger(self) -> None:
        for spelling, canonical in self.ALIAS_TABLE.items():
            (doc,) = normalize_spec_hooks(
                [
                    {
                        "name": "h",
                        "trigger": spelling,
                        "action": {"type": "command", "command": "/bin/true"},
                    }
                ]
            )
            assert doc["trigger"] == canonical, spelling

    def test_the_twelve_canonical_triggers_are_all_reachable(self) -> None:
        assert set(self.ALIAS_TABLE.values()) == {
            "SessionStart",
            "SessionEnd",
            "Stop",
            "PreToolUse",
            "PostToolUse",
            "PreTaskExec",
            "PostTaskExec",
            "UserPromptSubmit",
            "PostFileCreate",
            "PostFileSave",
            "PostFileDelete",
            "Manual",
        }

    def test_five_of_the_twelve_reach_kiro_cli(self) -> None:
        emitted = {}
        for canonical in set(self.ALIAS_TABLE.values()):
            document = {
                "name": "h",
                "trigger": canonical,
                "action": {"type": "command", "command": "/bin/true"},
            }
            for event in hook_documents_to_object_form(normalize_spec_hooks([document])):
                emitted[canonical] = event
        assert emitted == {
            "PreToolUse": "preToolUse",
            "PostToolUse": "postToolUse",
            "UserPromptSubmit": "userPromptSubmit",
            "SessionStart": "agentSpawn",
            "Stop": "stop",
        }


class TestObjectFormUnchanged:
    """The object form serializes for kiro-cli exactly as it always has."""

    def test_serialized_bytes_are_pinned(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply(
            {"preToolUse": [{"matcher": "*", "command": command}]},
            bundled={"postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}]},
        )
        expected = (
            '{"postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}], '
            '"preToolUse": [{"command": ' + json.dumps(command) + ', "matcher": "*"}]}'
        )
        assert json.dumps(hooks) == expected

    def test_extra_entry_keys_are_still_stripped(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply({"preToolUse": [{"command": command, "timeout_ms": 5, "cache_ttl": 9}]})
        assert hooks == {"preToolUse": [{"command": command}]}

    def test_object_form_does_not_go_through_the_normalizer(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The object form has one reader, the merge, so its bytes cannot drift."""

        def _fail(*_args, **_kwargs):
            raise AssertionError("the object form must not be re-derived from documents")

        monkeypatch.setattr(agent_mod, "normalize_spec_hooks", _fail)
        monkeypatch.setattr(agent_mod, "hook_documents_to_object_form", _fail)
        command = _hook_script(tmp_path)
        hooks = _apply({"preToolUse": [{"matcher": "*", "command": command}]})
        assert hooks == {"preToolUse": [{"command": command, "matcher": "*"}]}

    def test_the_normalizer_does_not_read_the_object_form(self, tmp_path: Path, sel_calls) -> None:
        """One reader owns the object form, so there is no second one to drift.

        Every rule about a command, a matcher, dedup and the caps lives in the
        merge. A normalizer that also read the object form would either restate
        those rules or move where they are applied.
        """
        command = _hook_script(tmp_path)
        assert normalize_spec_hooks({"preToolUse": [{"matcher": "*", "command": command}]}) == []
        assert [c[2] for c in sel_calls] == ["hooks is not an array of hook documents"]

    def test_object_form_bucket_faults_are_audited_by_the_merge(self, sel_calls) -> None:
        assert _apply({"preToolUse": "not-a-list"}) == {}
        assert _apply({"bogusEvent": [{"command": "/bin/true"}]}) == {}
        reasons = [c[2] for c in sel_calls]
        assert "entries not a list" in reasons
        assert "unknown event type" in reasons


class TestANullOptionalReadsAsAbsent:
    """`"matcher": null` is how JSON spells an omitted optional."""

    def _document(self, **extra: object) -> dict:
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": "/bin/true"},
        }
        document.update(extra)
        return document

    @pytest.mark.parametrize("field", ["matcher", "description", "timeout", "enabled", "confirm"])
    def test_a_null_optional_keeps_the_document(self, field: str) -> None:
        (doc,) = normalize_spec_hooks([self._document(**{field: None})])
        assert field not in doc
        assert doc["action"] == {"type": "command", "command": "/bin/true"}

    def test_a_null_matcher_still_reaches_the_kiro_cli_spec(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        document = self._document(matcher=None)
        document["action"] = {"type": "command", "command": command}
        assert _apply([document]) == {"preToolUse": [{"command": command}]}

    def test_a_null_name_is_synthesized(self) -> None:
        (doc,) = normalize_spec_hooks([self._document(name=None)])
        assert doc["name"] == "PreToolUse-0"

    def test_a_null_action_is_still_rejected(self, sel_calls) -> None:
        assert normalize_spec_hooks([self._document(action=None)]) == []
        assert [c[2] for c in sel_calls] == ["action is not an object"]


class TestLessExecutionIsNotDropped:
    """`enabled` and `confirm` grant less execution, so neither is dropped."""

    def _document(self, command: str, **extra: object) -> dict:
        document = {
            "name": "guard",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
        }
        document.update(extra)
        return document

    def test_a_disabled_hook_is_not_emitted(self, tmp_path: Path, caplog, sel_calls) -> None:
        command = _hook_script(tmp_path)
        document = self._document(command, enabled=False)
        assert normalize_spec_hooks([document])[0]["enabled"] is False
        with caplog.at_level(logging.INFO, logger="kiro_crew.agent"):
            assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert "disabled" in caplog.text
        assert [c[2] for c in sel_calls] == ["hook is disabled"]
        assert _apply([document]) == {}

    def test_an_enabled_hook_is_emitted(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        assert _apply([self._document(command, enabled=True)]) == {
            "preToolUse": [{"command": command}]
        }

    def test_a_hook_asking_for_confirmation_is_not_emitted(
        self, tmp_path: Path, caplog, sel_calls
    ) -> None:
        command = _hook_script(tmp_path)
        document = self._document(command, confirm=True)
        assert normalize_spec_hooks([document])[0]["confirm"] is True
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert "confirmed" in caplog.text
        assert [c[2] for c in sel_calls] == ["hook asks to be confirmed"]
        assert _apply([document]) == {}

    def test_confirm_false_is_emitted(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        assert _apply([self._document(command, confirm=False)]) == {
            "preToolUse": [{"command": command}]
        }

    def test_a_dropped_timeout_is_named(self, tmp_path: Path, caplog) -> None:
        """The command still runs, under a wider bound than the author asked for."""
        command = _hook_script(tmp_path)
        document = {
            "name": "guard",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
            "timeout": 5,
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            emitted = hook_documents_to_object_form(normalize_spec_hooks([document]))
        assert emitted == {"preToolUse": [{"command": command}]}, "the hook still runs"
        assert "timeout" in caplog.text
        assert "kiro-cli's own bound" in caplog.text

    def test_no_timeout_is_quiet(self, tmp_path: Path, caplog) -> None:
        command = _hook_script(tmp_path)
        document = {
            "name": "guard",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {
                "preToolUse": [{"command": command}]
            }
        assert "timeout" not in caplog.text

    def test_an_inexpressible_hook_warns_rather_than_informs(self, caplog, sel_calls) -> None:
        """Audited like every other branch that keeps a configured hook out."""
        document = {
            "name": "later",
            "trigger": "Manual",
            "action": {"type": "agent", "prompt": "summarize"},
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert "no kiro-cli hook" in caplog.text
        assert [c[2] for c in sel_calls] == ["no kiro-cli hook event can express this hook"]

    @pytest.mark.parametrize("trigger", ["PostFileSave", "Manual", "PreTaskExec"])
    def test_an_unmapped_trigger_is_audited_too(self, tmp_path: Path, sel_calls, trigger) -> None:
        command = _hook_script(tmp_path)
        document = {
            "name": "h",
            "trigger": trigger,
            "action": {"type": "command", "command": command},
        }
        assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert [c for c in sel_calls if c[2] == "no kiro-cli hook event can express this hook"]
        assert [c for c in sel_calls if c[0] == trigger]


class TestOffStaysOffThroughAutoimport:
    """Autoimport scans `~/.kiro/hooks`, so "off" has to hold there too.

    A document naming a script under that directory is left out of the emission,
    and the same script is then discovered on disk. Without suppression the merge
    has no dedup key for it, so the hook lands on autoimport's default event —
    broader than the one the document named.
    """

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path: Path, monkeypatch):
        """Point `Path.home()` at tmp_path so the hooks dir passes containment.

        Production rejects a `kiro_hooks_dir` that does not resolve under
        `Path.home()`, so a tmp dir is refused and autoimport silently scans
        nothing — which would let every test here pass with the suppression
        deleted. The same isolation `test_agent.py`'s autoimport class uses.
        """
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _run(self, tmp_path: Path, document: dict, script: Path) -> dict:
        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": [document],
                "kiro_hooks_autoimport": True,
                "kiro_hooks_dir": str(script.parent),
            }
        }
        _apply_user_kiro_hooks(config, mc_cfg)
        return config["hooks"]

    def _script(self, tmp_path: Path) -> Path:
        script = tmp_path / "hooks" / "guard.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        return script

    def _document(self, script: Path, **extra: object) -> dict:
        document = {
            "name": "guard",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": str(script)},
        }
        document.update(extra)
        return document

    def test_autoimport_discovers_the_script_at_all(self, tmp_path: Path) -> None:
        """The premise of this class: the scan reaches the directory.

        Asserted against autoimport alone, with no document naming the script,
        so a containment refusal fails here rather than quietly emptying the
        cases below.
        """
        script = self._script(tmp_path)
        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_autoimport": True, "kiro_hooks_dir": str(script.parent)}}
        _apply_user_kiro_hooks(config, mc_cfg)
        assert config["hooks"].get("preToolUse") == [{"command": str(script)}]

    def test_autoimport_arms_the_script_when_the_hook_is_on(self, tmp_path: Path) -> None:
        script = self._script(tmp_path)
        hooks = self._run(tmp_path, self._document(script), script)
        assert hooks.get("preToolUse") == [{"command": str(script)}]

    def test_a_disabled_hook_is_not_re_armed_by_autoimport(self, tmp_path: Path) -> None:
        script = self._script(tmp_path)
        hooks = self._run(tmp_path, self._document(script, enabled=False), script)
        assert not any(hooks.values()), f"the script came back: {hooks}"

    def test_a_confirm_hook_is_not_re_armed_by_autoimport(self, tmp_path: Path) -> None:
        script = self._script(tmp_path)
        hooks = self._run(tmp_path, self._document(script, confirm=True), script)
        assert not any(hooks.values()), f"the script came back: {hooks}"

    @pytest.mark.parametrize(
        "flaw",
        [
            {"matcher": "@git/status"},
            {"matcher": "fsWrite|fsAppend"},
            {"timeout": 0},
            {"description": 7},
            {"trigger": "NotATrigger"},
        ],
    )
    def test_an_invalid_off_document_still_suppresses(self, tmp_path: Path, flaw: dict) -> None:
        """Off is legible from two fields; the rest is the validator's business.

        A document that says `enabled: false` and is then rejected for something
        unrelated never becomes a document, so reading the suppressed set after
        validation would let autoimport arm its script unscoped.
        """
        script = self._script(tmp_path)
        document = self._document(script, enabled=False)
        document.update(flaw)
        assert normalize_spec_hooks([document]) == [], "the document is indeed rejected"
        assert hook_documents_suppressed_commands([document]) == {
            str(script.resolve()): agent_mod._HOOK_SUPPRESSED_DISABLED
        }
        hooks = self._run(tmp_path, document, script)
        assert not any(hooks.values()), f"the script came back: {hooks}"

    def test_an_invalid_confirm_document_still_suppresses(self, tmp_path: Path) -> None:
        script = self._script(tmp_path)
        document = self._document(script, confirm=True)
        document["matcher"] = "@git/status"
        assert normalize_spec_hooks([document]) == []
        hooks = self._run(tmp_path, document, script)
        assert not any(hooks.values()), f"the script came back: {hooks}"

    def test_a_confirmed_hook_is_audited_as_confirmed_not_disabled(
        self, tmp_path: Path, sel_calls
    ) -> None:
        """One filter, two causes, and the audit line has to say which.

        `enabled: false` and `confirm: true` both keep a discovered script out of
        the install, so a single hardcoded reason records a confirmation-gated
        hook as one the author switched off. An audit trail that names the wrong
        cause is worse than one that names none.
        """
        script = self._script(tmp_path)
        document = self._document(script, confirm=True)
        document["matcher"] = "@git/status"  # rejected, so only the raw read sees it
        assert hook_documents_suppressed_commands([document]) == {
            str(script.resolve()): agent_mod._HOOK_SUPPRESSED_CONFIRM
        }
        self._run(tmp_path, document, script)
        reasons = [c[2] for c in sel_calls if c[2].startswith("suppressed by")]
        assert reasons == [agent_mod._HOOK_SUPPRESSED_CONFIRM], sel_calls

    def test_disabled_outranks_confirm_on_the_same_command(self, tmp_path: Path) -> None:
        """Order in the array must not decide which cause is recorded."""
        script = self._script(tmp_path)
        off = self._document(script, enabled=False)
        gated = self._document(script, confirm=True)
        expected = {str(script.resolve()): agent_mod._HOOK_SUPPRESSED_DISABLED}
        assert hook_documents_suppressed_commands([off, gated]) == expected
        assert hook_documents_suppressed_commands([gated, off]) == expected
        both = self._document(script, enabled=False)
        both["confirm"] = True
        assert hook_documents_suppressed_commands([both]) == expected

    def test_an_invalid_document_that_is_on_suppresses_nothing(self, tmp_path: Path) -> None:
        script = self._script(tmp_path)
        document = self._document(script)
        document["matcher"] = "@git/status"
        assert hook_documents_suppressed_commands([document]) == {}

    @pytest.mark.parametrize("value", ["off", 0, None, 1])
    def test_only_a_real_false_suppresses(self, tmp_path: Path, value: object) -> None:
        """A truthy-looking value is not the author saying off."""
        script = self._script(tmp_path)
        document = self._document(script)
        document["enabled"] = value
        assert hook_documents_suppressed_commands([document]) == {}

    def test_the_suppressed_set_is_resolved_and_scoped(self, tmp_path: Path) -> None:
        script = self._script(tmp_path)
        on = self._document(script)
        off = self._document(script, enabled=False)
        assert hook_documents_suppressed_commands(normalize_spec_hooks([on])) == {}
        assert hook_documents_suppressed_commands(normalize_spec_hooks([off])) == {
            str(script.resolve()): agent_mod._HOOK_SUPPRESSED_DISABLED
        }

    def test_a_tilde_path_still_suppresses_the_script(self, tmp_path: Path, monkeypatch) -> None:
        """`~/.kiro/hooks/guard.sh` is the natural way to write it.

        Autoimport emits absolute resolved paths, so a command left unexpanded
        resolves against the cwd, matches nothing, and the switched-off script
        comes back.
        """
        script = self._script(tmp_path)
        document = self._document(script, enabled=False)
        # `expanduser` reads HOME, which the class fixture does not set: it pins
        # `Path.home()`. Both are the same directory in production, so the test
        # pins both rather than relying on one.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        document["action"] = {"type": "command", "command": "~/hooks/guard.sh"}
        assert hook_documents_suppressed_commands(normalize_spec_hooks([document])) == {
            str(Path(os.path.expanduser("~/hooks/guard.sh")).resolve()): (
                agent_mod._HOOK_SUPPRESSED_DISABLED
            )
        }
        hooks = self._run(tmp_path, document, script)
        assert not any(hooks.values()), f"the script came back: {hooks}"

    def test_a_symlink_loop_command_does_not_abort_the_merge(self, tmp_path: Path) -> None:
        """The validator resolves too, and its raise reaches the whole install.

        An absolute, allowlist-clean command pointing at a looping symlink gets
        past every earlier check and reaches `Path.resolve()` inside
        `_validate_hook_command`, whose caller is the agent-install pass.
        """
        loop = tmp_path / "vloop.sh"
        other = tmp_path / "vloop2.sh"
        loop.symlink_to(other)
        other.symlink_to(loop)
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": str(loop)},
        }
        hooks = _apply([document])
        assert not any(hooks.values()), "the hook is refused, not installed"

    def test_a_symlink_loop_does_not_abort_the_pass(self, tmp_path: Path) -> None:
        """Resolution raises on a loop; one lost suppression is not an abort."""
        loop = tmp_path / "loop.sh"
        loop.symlink_to(tmp_path / "loop2.sh")
        (tmp_path / "loop2.sh").symlink_to(loop)
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": str(loop)},
            "enabled": False,
        }
        assert hook_documents_suppressed_commands(normalize_spec_hooks([document])) == {}

    def test_a_document_past_the_cap_suppresses_nothing(self, tmp_path: Path) -> None:
        """A document the log calls ignored must not still delete a script."""
        script = self._script(tmp_path)
        filler = [
            {
                "name": f"f{i}",
                "trigger": "PreToolUse",
                "action": {"type": "command", "command": "/bin/true"},
            }
            for i in range(_MAX_SPEC_HOOK_DOCUMENTS)
        ]
        beyond = self._document(script, enabled=False)
        assert hook_documents_suppressed_commands(filler + [beyond]) == {}
        assert hook_documents_suppressed_commands([beyond] + filler) == {
            str(script.resolve()): agent_mod._HOOK_SUPPRESSED_DISABLED
        }

    @staticmethod
    def _on_windows_without_consent(monkeypatch) -> None:
        """Arm the platform and consent terms of the share gate.

        The gate is ``IS_WINDOWS and is_unc_shape(x) and not unc_probe_allowed(x)``
        — the spelling every UNC gate in this tree uses. A test that leaves the
        first two terms to the host would pass on Linux for an unrelated reason
        (the POSIX command allowlist rejects a backslash, and a share path does
        not exist), and so would still pass with the gate deleted.
        """
        monkeypatch.setattr(agent_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(agent_mod, "unc_probe_allowed", lambda raw: False)

    @pytest.mark.parametrize(
        "command",
        [
            r"\\attacker\share\guard.sh",
            "//attacker/share/guard.sh",
            r"\\?\UNC\attacker\share\guard.sh",
            r"\\.\pipe\guard.sh",
        ],
    )
    def test_a_network_path_is_never_resolved(self, sel_calls, monkeypatch, command: str) -> None:
        """Resolving a UNC path authenticates outbound on Windows.

        The command comes from an LLM-writable config, so resolving one would
        hand credentials to a host of someone else's choosing. The shape is
        refused lexically, before the resolve this PR's suppression read performs.
        """
        self._on_windows_without_consent(monkeypatch)
        assert agent_mod._hook_command_reaches_a_share(command) is True
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "enabled": False,
            "action": {"type": "command", "command": command},
        }
        # The suppression read is the one place this PR resolves a command the
        # author wrote, so it is the one place the gate belongs.
        assert hook_documents_suppressed_commands([document]) == {}

    def test_a_tilde_that_expands_to_a_share_is_refused(self, monkeypatch) -> None:
        """The expanded form is judged too, so `~` cannot smuggle a share in."""
        self._on_windows_without_consent(monkeypatch)
        monkeypatch.setenv("HOME", r"\\attacker\share")
        monkeypatch.setenv("USERPROFILE", r"\\attacker\share")
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "enabled": False,
            "action": {"type": "command", "command": "~/guard.sh"},
        }
        assert hook_documents_suppressed_commands([document]) == {}

    def test_the_shared_validator_does_not_consult_the_share_gate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`_validate_hook_command` is the object form's and autoimport's path.

        Both reach it having already resolved, or intending to resolve, a path
        the user chose: autoimport hands it an entry it resolved and stat-ed, so
        no probe is left to prevent, and a Windows host whose hooks directory
        lives on a share would have every hook refused. The gate is the new
        pre-resolution read's, and only its.
        """
        monkeypatch.setattr(agent_mod, "_hook_command_reaches_a_share", lambda command: True)
        script = str(self._script(tmp_path))
        assert agent_mod._validate_hook_command(script, "preToolUse") == str(Path(script).resolve())

    @pytest.mark.parametrize(
        "command",
        ["//host/share/guard.sh", r"\\host\share\guard.sh"],
    )
    def test_a_share_shape_is_not_refused_off_windows(self, monkeypatch, command: str) -> None:
        """The probe this gate prevents is a Windows one, so the gate is too.

        On POSIX a leading ``//`` is an ordinary path and a backslash spelling is
        one filename: ``resolve`` there opens no SMB session. Refusing either
        would reject a legal command for a risk the platform does not have, and
        would diverge from every sibling gate, all of which are ``os.name`` scoped.
        """
        monkeypatch.setattr(agent_mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(agent_mod, "unc_probe_allowed", lambda raw: False)
        assert agent_mod._hook_command_reaches_a_share(command) is False

    def test_a_consented_share_is_admitted(self, monkeypatch) -> None:
        """``unc_probe_allowed`` is the user's own consent, not an oversight.

        A roaming profile whose data home IS a share has to be able to run a hook
        script that lives on it; the gate admits exactly the shares that helper
        names and no others.
        """
        monkeypatch.setattr(agent_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(agent_mod, "unc_probe_allowed", lambda raw: True)
        assert agent_mod._hook_command_reaches_a_share(r"\\fileserver\home\me\guard.sh") is False

    def test_an_oversized_command_suppresses_nothing(self) -> None:
        """The set is retained, so the string it holds carries the same bound."""
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "enabled": False,
            "action": {"type": "command", "command": "/" + "x" * _MAX_HOOK_PAYLOAD_LEN},
        }
        assert hook_documents_suppressed_commands([document]) == {}

    def test_a_suppressed_script_is_audited(self, tmp_path: Path, sel_calls) -> None:
        """A discovered hook that stops being installed leaves a record.

        The emission path's own audit cannot cover this: an off-document rejected
        for an unrelated field never reaches it.
        """
        script = self._script(tmp_path)
        document = self._document(script, enabled=False)
        document["matcher"] = "@git/status"
        self._run(tmp_path, document, script)
        assert [c for c in sel_calls if c[2] == "suppressed by a disabled spec document"], sel_calls

    def test_an_agent_action_suppresses_nothing(self) -> None:
        document = {
            "name": "ask",
            "trigger": "Manual",
            "action": {"type": "agent", "prompt": "summarize"},
            "enabled": False,
        }
        assert hook_documents_suppressed_commands(normalize_spec_hooks([document])) == {}


class TestTheAuditCountsWhatWasWritten:
    """The merge summary's `requested_explicit` is the author's count."""

    def _summary(self, monkeypatch, kiro_hooks: object) -> str:
        events: list[object] = []

        class _Sel:
            def log(self, event):
                events.append(event)

            def log_api_access(self, **_kwargs):
                pass

        monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())
        config: dict = {"hooks": {}}
        _apply_user_kiro_hooks(config, _mc_cfg(kiro_hooks))
        merges = [e for e in events if getattr(e, "operation", "") == "kiro_hooks_merge"]
        assert merges, "the merge summary is emitted"
        return str(getattr(merges[-1], "resources", ""))

    def test_an_array_reports_the_documents_not_the_projection(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Seven of these reach no kiro-cli event; all nine were requested."""
        command = _hook_script(tmp_path)
        expressible = [
            {"name": f"e{i}", "trigger": t, "action": {"type": "command", "command": command}}
            for i, t in enumerate(["PreToolUse", "PostToolUse"])
        ]
        inert = [
            {"name": f"i{i}", "trigger": t, "action": {"type": "command", "command": command}}
            for i, t in enumerate(
                ["SessionEnd", "Manual", "PreTaskExec", "PostTaskExec", "PostFileSave"]
            )
        ]
        inert.append(
            {
                "name": "agenty",
                "trigger": "PreToolUse",
                "action": {"type": "agent", "prompt": "look"},
            }
        )
        inert.append(
            {
                "name": "off",
                "trigger": "PreToolUse",
                "enabled": False,
                "action": {"type": "command", "command": command},
            }
        )
        summary = self._summary(monkeypatch, expressible + inert)
        assert "requested_explicit=9" in summary, summary

    def test_a_rejected_document_still_counts_as_requested(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The author wrote it, so the audit says it was asked for."""
        command = _hook_script(tmp_path)
        good = {
            "name": "g",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
        }
        rejected = {
            "name": "r",
            "trigger": "NotATrigger",
            "action": {"type": "command", "command": command},
        }
        summary = self._summary(monkeypatch, [good, rejected])
        assert "requested_explicit=2" in summary, summary

    def test_the_object_form_still_counts_entries(self, tmp_path: Path, monkeypatch) -> None:
        command = _hook_script(tmp_path)
        summary = self._summary(
            monkeypatch,
            {"preToolUse": [{"command": command}], "postToolUse": [{"command": command}]},
        )
        assert "requested_explicit=2" in summary, summary


class TestSharedErrorPath:
    """One defect, two shapes, one rejection."""

    def _object(self, command: str, matcher: str | None = None) -> dict:
        entry: dict = {"command": command}
        if matcher is not None:
            entry["matcher"] = matcher
        return {"preToolUse": [entry]}

    def _array(self, command: str, matcher: str | None = None) -> list[dict]:
        document: dict = {
            "name": "h",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
        }
        if matcher is not None:
            document["matcher"] = matcher
        return [document]

    def test_relative_command_rejected_in_both_shapes(self, sel_calls) -> None:
        assert _apply(self._object("hook.sh")) == {"preToolUse": []}
        object_reasons = [c[2] for c in sel_calls]
        sel_calls.clear()
        assert _apply(self._array("hook.sh")) == {"preToolUse": []}
        assert [c[2] for c in sel_calls] == object_reasons == ["failed validation"]

    def test_missing_command_file_rejected_in_both_shapes(self, tmp_path: Path, sel_calls) -> None:
        missing = str(tmp_path / "absent.sh")
        assert _apply(self._object(missing)) == {"preToolUse": []}
        object_reasons = [c[2] for c in sel_calls]
        sel_calls.clear()
        assert _apply(self._array(missing)) == {"preToolUse": []}
        assert [c[2] for c in sel_calls] == object_reasons == ["failed validation"]

    def test_bad_matcher_rejected_in_both_shapes(self, tmp_path: Path, sel_calls) -> None:
        """Same reason, same dropped hook. The empty bucket is a shape artifact.

        An object-form read keeps the event bucket it was given and drops the
        entry inside it; an array-form read has no bucket to keep, so the event
        does not appear at all. What is pinned is the rejection and its audit
        reason, which are one and the same.
        """
        command = _hook_script(tmp_path)
        assert _apply(self._object(command, matcher="rm -rf /")) == {"preToolUse": []}
        assert [c[2] for c in sel_calls] == ["invalid matcher"]
        sel_calls.clear()
        assert _apply(self._array(command, matcher="rm -rf /")) == {}
        assert [c[2] for c in sel_calls] == ["invalid matcher"]

    def test_caps_apply_to_the_array_form(self, tmp_path: Path) -> None:
        documents = [
            {
                "name": f"h{index}",
                "trigger": "PreToolUse",
                "action": {"type": "command", "command": _hook_script(tmp_path, f"h{index}.sh")},
            }
            for index in range(agent_mod._MAX_USER_HOOKS_PER_EVENT + 4)
        ]
        hooks = _apply(documents)
        assert len(hooks["preToolUse"]) == agent_mod._MAX_USER_HOOKS_PER_EVENT

    def test_dedup_applies_to_the_array_form(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
        }
        hooks = _apply([document, dict(document, name="h2")])
        assert hooks == {"preToolUse": [{"command": command}]}


class TestDiagnosticsCarryNoSecret:
    """A rejected value reaches a log line redacted, and SEL whole.

    `gateway.log` persists and rotates rather than expires, and a spec is
    LLM-writable, so a value quoted in a diagnostic can be a credential. The
    audit writer redacts before it truncates, so a caller that pre-slices hands
    the redactors a value already cut at an arbitrary boundary.
    """

    def _document(self, trigger: object) -> dict:
        return {
            "name": "h",
            "trigger": trigger,
            "action": {"type": "command", "command": "/bin/true"},
        }

    def test_a_rejected_trigger_is_redacted_before_it_is_logged(
        self, monkeypatch, sel_calls, caplog
    ) -> None:
        monkeypatch.setattr(agent_mod, "redact_log", lambda value: "[SCRUBBED]")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert normalize_spec_hooks([self._document("AKIAIOSFODNN7EXAMPLE")]) == []
        assert "[SCRUBBED]" in caplog.text
        assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text

    def test_a_rejected_action_type_is_redacted_before_it_is_logged(
        self, monkeypatch, sel_calls, caplog
    ) -> None:
        monkeypatch.setattr(agent_mod, "redact_log", lambda value: "[SCRUBBED]")
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "AKIAIOSFODNN7EXAMPLE", "command": "/bin/true"},
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert normalize_spec_hooks([document]) == []
        assert "[SCRUBBED]" in caplog.text
        assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text

    @pytest.mark.parametrize(
        "command",
        [
            "AKIAIOSFODNN7EXAMPLE secret",
            "AKIAIOSFODNN7EXAMPLE",
            "/nonexistent/AKIAIOSFODNN7EXAMPLE",
        ],
    )
    def test_a_rejected_command_is_redacted_before_it_is_logged(
        self, monkeypatch, sel_calls, caplog, command: str
    ) -> None:
        """The command validator quotes the value it refuses, so it redacts it.

        Its three ordinary refusals are a path with disallowed characters, a
        relative path, and a path that is not there. A command reaches it from
        either shape.
        """
        monkeypatch.setattr(agent_mod, "redact_log", lambda value: "[SCRUBBED]")
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            hooks = _apply([document])
        assert not any(hooks.values()), "the command is refused, so no entry is emitted"
        assert "[SCRUBBED]" in caplog.text
        assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text

    def test_a_logged_value_cannot_forge_a_second_record(self, sel_calls, caplog) -> None:
        """A newline in a config value must not close the log record.

        Redaction substitutes credential patterns and leaves control characters
        alone, so the escape has to come first: without it one config value
        writes a second physical line that reads like a real gateway record.
        """
        forged = "/bin/x\n2026-01-01 00:00:00 WARNING kiro_hooks: approved /bin/evil"
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert _apply(
                [
                    {
                        "name": "h",
                        "trigger": "PreToolUse",
                        "action": {"type": "command", "command": forged},
                    }
                ]
            ) == {"preToolUse": []}
        assert "approved /bin/evil" in caplog.text, "the value is still reported"
        assert "\n2026-01-01" not in caplog.text, "but not as its own record"
        for record in caplog.records:
            assert "\n" not in record.getMessage()

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Win32 rejects a newline in a filename, so the forged input cannot exist there",
    )
    def test_a_scanned_filename_cannot_forge_a_record(
        self, tmp_path: Path, sel_calls, caplog
    ) -> None:
        """The scan quotes a filename an author controls, so it escapes it too.

        The rule stated above this module's ``_hook_diagnostic`` covers the whole
        hook path, and the scan is on it: a file in the hooks directory is named
        by whoever can write there, and a newline in that name would close the
        record and write a second one that reads like a real gateway line.

        The escape itself is unconditional; only this test is POSIX-only, because
        a filename is the one hook-path value Windows cannot carry a newline in.
        """
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        forged = hooks_dir / "x\n2026-01-01 00:00:00 WARNING kiro_hooks: approved evil.sh"
        forged.write_text("#!/bin/sh\nexit 0\n")
        forged.chmod(0o644)  # not executable -> the scan reports the skip
        with caplog.at_level(logging.INFO, logger="kiro_crew.agent"):
            assert agent_mod._autoimport_kiro_hooks(hooks_dir) == {}
        assert "approved evil.sh" in caplog.text, "the filename is still reported"
        for record in caplog.records:
            assert "\n" not in record.getMessage()

    @staticmethod
    def _composition_fails(monkeypatch) -> None:
        """Put the host in the state the egress redactor refuses to serve.

        A context IS installed and its policy cannot be composed — the one state
        `redact_via_context` re-raises on. Driven through the real shim rather
        than by stubbing the module's own redactor, so what the test observes is
        the platform contract and not a local fake.
        """
        from kiro_crew.platform import context as ctx

        def _refuse(_text: str) -> str:
            raise ctx.PlatformCompositionError("policy could not be composed")

        monkeypatch.setattr(ctx, "installed_context", lambda: object())
        monkeypatch.setattr(ctx, "redact_via_context", _refuse)

    def test_a_redaction_policy_failure_does_not_abort_the_install(
        self, monkeypatch, sel_calls, caplog
    ) -> None:
        """A diagnostic is not an egress sink, so it must not raise.

        The egress spelling refuses to compose rather than downgrade, which is
        right for a sink that must not send. These call sites are un-wrapped log
        arguments on the agent-install path, so the same raise would abort the
        install over a log line.
        """
        from kiro_crew.platform.context import LOG_WITHHELD_PLACEHOLDER

        self._composition_fails(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert (
                _apply(
                    [
                        {
                            "name": "h",
                            "trigger": "Nope",
                            "action": {"type": "command", "command": "/bin/true"},
                        }
                    ]
                )
                == {}
            )
        assert "unknown trigger" in caplog.text
        assert LOG_WITHHELD_PLACEHOLDER in caplog.text, "the value is withheld, not raw"

    def test_a_redaction_policy_failure_does_not_lose_the_audit(self, monkeypatch, caplog) -> None:
        """The audit's own argument cannot be built by something that refuses.

        `_sel_hook_rejected` exists to write the rejection record, so composing
        its `resources` with the egress redactor made the record the first thing
        lost on a host whose policy cannot be composed. The log-only spelling
        withholds the quoted value instead — never emits it raw — so the record is
        still written and still carries its reason.
        """
        from kiro_crew.platform.context import LOG_WITHHELD_PLACEHOLDER

        logged: list[object] = []

        class _Sel:
            def log(self, event):
                logged.append(event)

            def log_api_access(self, **_kwargs):
                pass

        self._composition_fails(monkeypatch)
        monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert normalize_spec_hooks([{"name": "h", "trigger": "Stop", "action": "nope"}]) == []
        reasons = [getattr(event, "error", None) for event in logged]
        assert "action is not an object" in reasons, logged
        resources = [getattr(event, "resources", "") for event in logged]
        assert all(LOG_WITHHELD_PLACEHOLDER in value for value in resources), resources
        # Each value is withheld on its own, so the field still says which value
        # is which: one substitution over the interpolated field would lose the
        # shape as well as both values.
        assert all(
            value.startswith("event=") and " command=" in value for value in resources
        ), resources

    def test_the_audit_value_is_passed_whole(self, sel_calls) -> None:
        """Unsliced: the writer redacts first, so the cut must be its own."""
        trigger = "x" * 500
        assert normalize_spec_hooks([self._document(trigger)]) == []
        _event, value, reason = sel_calls[0]
        assert reason == "unknown trigger"
        assert value == trigger

    def test_an_inexpressible_hook_name_is_redacted(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(agent_mod, "redact_log", lambda value: "[SCRUBBED]")
        document = {
            "name": "AKIAIOSFODNN7EXAMPLE",
            "trigger": "Manual",
            "action": {"type": "command", "command": "/bin/true"},
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert "[SCRUBBED]" in caplog.text
        assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text


class TestArrayFormGuards:
    """Each guard the array form adds, and the input that trips it.

    Every test here fails if its guard is removed: the rejected input would
    become an accepted hook document.
    """

    def test_element_must_be_an_object(self, sel_calls) -> None:
        assert normalize_spec_hooks(["/bin/true"]) == []
        assert [c[2] for c in sel_calls] == ["hook document is not an object"]

    def test_trigger_must_be_known(self, sel_calls) -> None:
        assert (
            normalize_spec_hooks(
                [
                    {
                        "name": "h",
                        "trigger": "WheneverIFeelLikeIt",
                        "action": {"type": "command", "command": "/bin/true"},
                    }
                ]
            )
            == []
        )
        assert [c[2] for c in sel_calls] == ["unknown trigger"]

    def test_trigger_must_be_a_string(self, sel_calls) -> None:
        assert (
            normalize_spec_hooks(
                [{"name": "h", "trigger": 7, "action": {"type": "command", "command": "/bin/true"}}]
            )
            == []
        )
        assert [c[2] for c in sel_calls] == ["unknown trigger"]

    def test_action_must_be_an_object(self, sel_calls) -> None:
        assert normalize_spec_hooks([{"name": "h", "trigger": "Stop", "action": "/bin/true"}]) == []
        assert [c[2] for c in sel_calls] == ["action is not an object"]

    def test_action_type_must_be_known(self, sel_calls) -> None:
        assert (
            normalize_spec_hooks(
                [
                    {
                        "name": "h",
                        "trigger": "Stop",
                        "action": {"type": "eval", "command": "/bin/true"},
                    }
                ]
            )
            == []
        )
        assert [c[2] for c in sel_calls] == ["unknown action type"]

    @pytest.mark.parametrize("command", [None, "", 7, ["/bin/true"]])
    def test_command_action_needs_a_command(self, sel_calls, command: object) -> None:
        action: dict = {"type": "command"}
        if command is not None:
            action["command"] = command
        assert normalize_spec_hooks([{"name": "h", "trigger": "Stop", "action": action}]) == []
        assert [c[2] for c in sel_calls] == ["command action without command"]

    @pytest.mark.parametrize("prompt", [None, "", 7])
    def test_agent_action_needs_a_prompt(self, sel_calls, prompt: object) -> None:
        action: dict = {"type": "agent"}
        if prompt is not None:
            action["prompt"] = prompt
        assert normalize_spec_hooks([{"name": "h", "trigger": "Stop", "action": action}]) == []
        assert [c[2] for c in sel_calls] == ["agent action without prompt"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("name", 7), ("description", 7), ("enabled", "yes"), ("confirm", "no")],
    )
    def test_optional_fields_are_type_checked(self, sel_calls, field: str, value: object) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            field: value,
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == [f"{field} has the wrong type"]

    def test_empty_name_is_rejected(self, sel_calls) -> None:
        document = {
            "name": "",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == ["name is empty"]

    def test_name_is_synthesized_when_absent(self) -> None:
        (doc,) = normalize_spec_hooks(
            [{"trigger": "Stop", "action": {"type": "command", "command": "/bin/true"}}]
        )
        assert doc["name"] == "Stop-0"

    @pytest.mark.parametrize("timeout", [0, -1, True, "30", 1.5])
    def test_timeout_must_be_a_positive_integer(self, sel_calls, timeout: object) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            "timeout": timeout,
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == ["timeout not a positive integer"]

    @pytest.mark.parametrize("matcher", ["rm -rf /", "a" * 201, 7])
    def test_matcher_follows_the_object_form_rules(self, sel_calls, matcher: object) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            "matcher": matcher,
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == ["invalid matcher"]

    def test_document_count_is_bounded(self, sel_calls, caplog) -> None:
        documents = [
            {
                "name": f"h{i}",
                "trigger": "Stop",
                "action": {"type": "command", "command": "/bin/true"},
            }
            for i in range(_MAX_SPEC_HOOK_DOCUMENTS + 5)
        ]
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            docs = normalize_spec_hooks(documents)
        assert len(docs) == _MAX_SPEC_HOOK_DOCUMENTS
        assert [c[2] for c in sel_calls] == ["document limit exceeded"]

    @pytest.mark.parametrize("action_type", [[], {}, 7, None])
    def test_action_type_must_be_a_string(self, sel_calls, action_type: object) -> None:
        """An unhashable type would raise out of a normalizer that must not raise."""
        action: dict = {"command": "/bin/true"}
        if action_type is not None:
            action["type"] = action_type
        assert normalize_spec_hooks([{"name": "h", "trigger": "Stop", "action": action}]) == []
        assert [c[2] for c in sel_calls] == ["unknown action type"]

    @pytest.mark.parametrize("action_type", ["command", "agent"])
    def test_action_payload_length_is_bounded(self, sel_calls, action_type: str) -> None:
        payload_key = "command" if action_type == "command" else "prompt"
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": action_type, payload_key: "x" * (_MAX_HOOK_PAYLOAD_LEN + 1)},
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == [f"{payload_key} too long"]

    @pytest.mark.parametrize(
        ("field", "limit"),
        [("name", _MAX_HOOK_NAME_LEN), ("description", _MAX_HOOK_DESCRIPTION_LEN)],
    )
    def test_retained_string_fields_are_bounded(self, sel_calls, field: str, limit: int) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            field: "x" * (limit + 1),
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == [f"{field} too long"]

    @pytest.mark.parametrize(
        ("field", "limit"),
        [("name", _MAX_HOOK_NAME_LEN), ("description", _MAX_HOOK_DESCRIPTION_LEN)],
    )
    def test_a_string_field_at_the_limit_is_kept(self, field: str, limit: int) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            field: "x" * limit,
        }
        (doc,) = normalize_spec_hooks([document])
        assert doc[field] == "x" * limit

    def test_one_bad_document_does_not_drop_the_good_ones(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply(
            [
                {
                    "name": "bad",
                    "trigger": "Nope",
                    "action": {"type": "command", "command": command},
                },
                {
                    "name": "good",
                    "trigger": "Stop",
                    "action": {"type": "command", "command": command},
                },
            ]
        )
        assert hooks == {"stop": [{"command": command}]}
