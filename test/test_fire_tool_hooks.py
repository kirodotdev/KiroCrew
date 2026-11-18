"""Tests for fire_tool_hooks helper and global hook store accessor."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redaction_structured_cases import (
    PEM_BEGIN,
    PEM_BODY,
    aws_key_id,
    aws_secret,
    github_classic,
    secret_cases,
)

from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    ScriptHookStore,
    fire_tool_hooks,
    get_global_hook_store,
    hook_tool_input,
    permission_pre_tool_block,
    run_script_hook,
    set_global_hook_store,
)

pytestmark = pytest.mark.xdist_group(name="fire_tool_hooks_probe")


@pytest.fixture(autouse=True)
def _reset_global_store():
    """Reset global hook store between tests."""
    set_global_hook_store(None)  # type: ignore[arg-type]
    yield
    set_global_hook_store(None)  # type: ignore[arg-type]


@pytest.fixture
def hook_store(tmp_path: Path) -> ScriptHookStore:
    return ScriptHookStore(tmp_path)


@pytest.fixture(scope="module")
def probe_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Share the immutable hook script; each case keeps its own output and cwd."""
    script = tmp_path_factory.mktemp("hook_probe") / "probe.py"
    script.write_text(
        "import os, sys\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as f:\n"
        "    f.write(sys.stdin.read())\n"
        "with open(sys.argv[1] + '.cwd', 'w', encoding='utf-8') as f:\n"
        "    f.write(os.getcwd())\n",
        encoding="utf-8",
    )
    return script


def _probe_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_script: Path):
    """A real hook command that writes its stdin event and its own cwd to files.

    Every hook child runs in ``tmp_path``: the real spawner is wrapped to pass
    ``cwd=tmp_path``, since its default inherits the test runner's working
    directory (the checkout). Returns ``(command, read)``; ``read()`` parses the
    event the child got and asserts the child ran in ``tmp_path``.
    """
    import kiro_crew.hooks as hooks_mod

    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)
    real_run = hooks_mod.run_script_hook

    async def run_in_tmp(*args, **kwargs):
        kwargs["cwd"] = str(tmp_path)
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(hooks_mod, "run_script_hook", run_in_tmp)
    out = tmp_path / "event.json"

    def read() -> dict:
        child_cwd = Path(f"{out}.cwd").read_text(encoding="utf-8")
        assert Path(child_cwd).resolve() == tmp_path.resolve()
        return json.loads(out.read_text(encoding="utf-8"))

    return f'"{sys.executable}" "{probe_script}" "{out}"', read


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_script: Path):
    """A store with one real PreToolUse hook that writes its stdin event to a file.

    Returns ``(store, read)``: ``read()`` parses the event the hook process got.
    """
    command, read = _probe_command(tmp_path, monkeypatch, probe_script)
    store = ScriptHookStore(tmp_path / "store")
    store.create({
        "name": "probe",
        "event": HOOK_EVENT_PRE_TOOL_USE,
        "matcher": "",
        "command": command,
    })
    return store, read


_ATTRIBUTION = ("subagent_id", "parent_session_key", "agent_role")


class TestGlobalHookStore:
    """Test get/set global hook store accessor."""

    def test_default_is_none(self):
        assert get_global_hook_store() is None

    def test_set_and_get(self, hook_store: ScriptHookStore):
        set_global_hook_store(hook_store)
        assert get_global_hook_store() is hook_store

    def test_overwrite(self, tmp_path: Path):
        store1 = ScriptHookStore(tmp_path / "a")
        store2 = ScriptHookStore(tmp_path / "b")
        set_global_hook_store(store1)
        set_global_hook_store(store2)
        assert get_global_hook_store() is store2


class TestFireToolHooks:
    """Test fire_tool_hooks helper."""

    @pytest.mark.asyncio
    async def test_none_store_is_noop(self):
        # Should not raise
        await fire_tool_hooks(None, "Running: echo hello")

    @pytest.mark.asyncio
    async def test_strips_running_prefix(self, probe):
        store, read = probe
        await fire_tool_hooks(store, "Running: echo hello")
        event = read()
        assert event["hook_event_name"] == HOOK_EVENT_PRE_TOOL_USE
        assert event["tool_name"] == "echo hello"
        assert "tool_input" not in event
        assert not set(_ATTRIBUTION) & set(event)

    @pytest.mark.asyncio
    async def test_no_prefix(self, probe):
        store, read = probe
        await fire_tool_hooks(store, "@builder-mcp/ReadFile")
        event = read()
        assert event["tool_name"] == "@builder-mcp/ReadFile"
        assert "tool_input" not in event

    @pytest.mark.asyncio
    async def test_parses_tool_input_json(self, probe):
        store, read = probe
        ti = json.dumps({"path": "/tmp/test.txt"})
        await fire_tool_hooks(store, "ReadFile", ti)
        event = read()
        assert event["tool_name"] == "ReadFile"
        assert event["tool_input"] == {"path": "/tmp/test.txt"}

    @pytest.mark.asyncio
    async def test_invalid_json_passes_none(self, probe):
        store, read = probe
        await fire_tool_hooks(store, "ReadFile", "not-json")
        event = read()
        assert event["tool_name"] == "ReadFile"
        assert "tool_input" not in event

    @pytest.mark.asyncio
    async def test_empty_title(self, probe):
        # An empty title still fires the hook; the payload carries no tool name.
        store, read = probe
        await fire_tool_hooks(store, "")
        event = read()
        assert event["hook_event_name"] == HOOK_EVENT_PRE_TOOL_USE
        assert "tool_name" not in event

    @pytest.mark.asyncio
    async def test_fire_exception_swallowed(self, hook_store: ScriptHookStore, caplog):
        caplog.set_level(logging.DEBUG, logger="kiro_crew.hooks")
        with patch.object(
            hook_store, "fire", new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ):
            await fire_tool_hooks(hook_store, "ReadFile")
        swallowed = [
            r for r in caplog.records
            if r.name == "kiro_crew.hooks" and r.getMessage() == "PreToolUse hook error"
        ]
        assert len(swallowed) == 1
        assert str(swallowed[0].exc_info[1]) == "boom"

    @pytest.mark.asyncio
    async def test_none_tool_input_skipped(self, probe):
        store, read = probe
        await fire_tool_hooks(store, "ReadFile", None)
        event = read()
        assert event["tool_name"] == "ReadFile"
        assert "tool_input" not in event

    @pytest.mark.asyncio
    async def test_passes_subagent_metadata(self, probe):
        """subagent_id, parent_session_key and agent_role reach the hook's stdin event."""
        store, read = probe
        await fire_tool_hooks(
            store,
            "ReadFile",
            None,
            subagent_id="abc12345",
            parent_session_key="dashboard:slot-1",
            agent_role="utility",
        )
        event = read()
        assert {k: event[k] for k in _ATTRIBUTION} == {
            "subagent_id": "abc12345",
            "parent_session_key": "dashboard:slot-1",
            "agent_role": "utility",
        }


class TestHookToolInput:
    """``hook_tool_input`` builds the payload's ``tool_input`` from the call's
    structured params, redacting value by value.

    Credentials come from the shared generators in ``redaction_structured_cases``,
    so no credential-shaped literal sits in the source as one token. A URL keeps
    its shape: only the secret value inside it is masked.
    """

    _TAG = "[REDACTED: credential]"
    # The token URL parameter is a rule-table row: its value is masked, the URL kept.
    _TOKEN = "a1b2c3d4" * 4
    _URL = "https://example.com/a?token=" + _TOKEN
    _MASKED_URL = "https://example.com/a?token=" + _TAG
    # An unterminated private-key block: the rule masks to the end of the text, so
    # in the display JSON it also swallows the closing quote and brace.
    _UNTERMINATED_KEY = f"printf '%s' '{PEM_BEGIN}\n{PEM_BODY}'"
    # Display text that does not parse, built directly.
    _UNPARSEABLE = '{\n  "command": "ls'

    @staticmethod
    def _display_json(params: dict) -> str:
        """The rendered display text as the tool_call parser produces it: the
        params dumped as JSON, then scrubbed as a whole."""
        from kiro_crew.acp._dispatch_parts.surfaced_text import redact_text

        return redact_text(json.dumps(params, indent=2))

    def test_an_unterminated_private_key_breaks_the_display_json(self):
        """Parsed display text: the key's mask runs past the value and eats the JSON's end."""
        rendered = self._display_json({"command": self._UNTERMINATED_KEY})
        assert self._TAG in rendered
        with pytest.raises(json.JSONDecodeError):
            json.loads(rendered)

    def test_an_unterminated_private_key_gives_no_tool_input(self):
        """The key's mask eats the dump's end, as it does on the approval card, so
        the hook gets no ``tool_input`` rather than more than the card shows."""
        params = {"command": self._UNTERMINATED_KEY}
        assert hook_tool_input(params, self._display_json(params)) is None

    def test_a_url_keeps_its_shape_with_only_the_secret_value_masked(self):
        params = {"command": f"curl -s --url '{self._URL}'"}
        got = hook_tool_input(params, self._display_json(params))
        assert got == {"command": f"curl -s --url '{self._MASKED_URL}'"}
        assert got == json.loads(self._display_json(params))  # same as the display path

    @pytest.mark.parametrize(
        "text",
        [case[1] for case in secret_cases()],
        ids=[case[0] for case in secret_cases()],
    )
    def test_nothing_the_display_path_masks_reaches_the_hook(self, text):
        """Every word of a rule-table case that the display path masks is masked
        in the hook's ``command`` too, so the hook never sees more than the card."""
        params = {"command": text}
        got = hook_tool_input(params, None)
        assert got is None or set(got) == {"command"}
        sent = json.dumps(got)
        rendered = self._display_json(params)
        try:
            shown = json.loads(rendered)["command"]
        except json.JSONDecodeError:
            shown = rendered  # an unterminated block: its mask ran to the end
        masked = [w for w in text.split() if len(w) >= 8 and w not in shown]
        for word in masked:
            assert json.dumps(word)[1:-1] not in sent, word

    @pytest.mark.parametrize("shape", ["list", "fields"])
    @pytest.mark.parametrize(
        "text",
        [case[1] for case in secret_cases()],
        ids=[case[0] for case in secret_cases()],
    )
    def test_a_case_split_across_siblings_masks_what_the_display_path_masks(self, text, shape):
        """Each rule-table case, its words spread over sibling list elements or
        sibling fields: every word the display path masks in that layout is masked
        for the hook too, a pair whose halves sit in different values included."""
        words = text.split()
        if shape == "list":
            params: dict = {"args": words}
        else:
            params = {f"f{i}": w for i, w in enumerate(words)}
        got = hook_tool_input(params, None)
        assert isinstance(got, dict)
        # Read as text: an unterminated block's mask runs to the end and breaks the JSON.
        shown = self._display_json(params)
        sent = json.dumps(got, indent=2)
        masked = [w for w in words if len(w) >= 8 and json.dumps(w)[1:-1] not in shown]
        for word in masked:
            assert json.dumps(word)[1:-1] not in sent, word

    def test_an_aws_secret_in_a_sibling_list_element_is_masked(self):
        """An unlabelled secret is masked only with its access-key ID in the same
        scanned text; here the two sit in different elements of one list."""
        from kiro_crew.platform.context import redact_via_context

        key_id, secret = aws_key_id(), aws_secret()
        assert redact_via_context(secret) == secret  # alone it is an opaque string
        params = {"args": ["--key", key_id, "--secret", secret]}
        assert secret not in self._display_json(params)
        got = hook_tool_input(params, None)
        assert got == {"args": ["--key", key_id, "--secret", self._TAG]}
        assert params["args"][3] == secret  # the caller's dict is not rewritten

    def test_an_aws_secret_in_a_sibling_field_is_masked(self):
        """The same pair in two fields whose names are not credential names."""
        key_id, secret = aws_key_id(), aws_secret()
        params = {"id": key_id, "region": "us-east-1", "value": secret}
        assert secret not in self._display_json(params)
        got = hook_tool_input(params, None)
        assert got == {"id": key_id, "region": "us-east-1", "value": self._TAG}

    def test_a_pair_beside_an_unterminated_private_key_gives_no_tool_input(self):
        """When a mask swallows the JSON's syntax, the hook gets no ``tool_input``,
        so neither the pair's secret nor the key body leaks."""
        key_id, secret = aws_key_id(), aws_secret()
        params = {"id": key_id, "value": secret, "command": self._UNTERMINATED_KEY}
        with pytest.raises(json.JSONDecodeError):
            json.loads(self._display_json(params))
        assert hook_tool_input(params, None) is None

    def test_a_number_under_a_listed_credential_name_never_reaches_the_hook(self):
        """The credential-name row masks a number with its key, and the masked text
        is not JSON: the hook gets no ``tool_input`` rather than the number."""
        number = 83920174
        params = {"env": {"DB_PASSWORD": number}}
        assert str(number) not in self._display_json(params)
        got = hook_tool_input(params, None)
        assert str(number) not in json.dumps(got)

    def test_a_loaded_policys_own_pattern_is_masked_in_hook_input(self, monkeypatch):
        """A credential format only the loaded credential policy knows is masked for
        the hook, after the card's pass and on the same text."""
        from kiro_crew.hooks import current_context

        marker = "xq" + "-site-" + "77aa11"
        policy = current_context().credentials
        base = policy.redact
        monkeypatch.setattr(
            policy, "redact", lambda text: base(text).replace(marker, "[REDACTED: credential]")
        )
        params = {"command": "curl -H 'X-Site: " + marker + "' https://example.com", "n": 1}
        assert marker in self._display_json(params)  # the card's pass alone keeps it
        got = hook_tool_input(params, None)
        assert got == {
            "command": "curl -H 'X-Site: [REDACTED: credential]' https://example.com",
            "n": 1,
        }

    def test_a_private_key_before_the_path_gives_no_tool_input(self):
        """A file write whose text holds an unterminated private-key block ahead of
        ``path``: the key's mask runs to the end of the dump, as on the approval
        card, so the hook gets no ``tool_input`` and no key body."""
        params = {
            "command": "create",
            "text": f"{PEM_BEGIN}\n{PEM_BODY}",
            "path": "/work/out.txt",
        }
        got = hook_tool_input(params, None)
        assert got is None
        for line in PEM_BODY.splitlines():
            assert line not in json.dumps(got)

    def test_a_key_id_before_a_private_key_still_masks_its_paired_secret(self):
        """A string replace whose old text holds an access-key ID and then an
        unterminated private-key block, with the paired secret in the new text:
        the secret never reaches the hook."""
        key_id, secret = aws_key_id(), aws_secret()
        params = {
            "command": "strReplace",
            "path": "/work/creds.txt",
            "oldStr": f"id {key_id}\n{PEM_BEGIN}\n{PEM_BODY}",
            "newStr": f"secret {secret}",
        }
        assert secret not in self._display_json(params)
        got = hook_tool_input(params, None)
        sent = json.dumps(got)
        assert secret not in sent
        for line in PEM_BODY.splitlines():
            assert line not in sent

    def test_a_key_id_in_a_partly_masked_value_still_pairs_with_its_secret(self):
        """A value with a masked URL token and an access-key ID: the whole-text pass
        still sees the ID, so it masks the secret in the sibling field."""
        key_id, secret = aws_key_id(), aws_secret()
        params = {"id": key_id + " " + self._URL, "value": secret}
        assert secret not in self._display_json(params)
        got = hook_tool_input(params, None)
        assert isinstance(got, dict) and set(got) == {"id", "value"}
        assert got["value"] == self._TAG
        assert secret not in json.dumps(got)

    @staticmethod
    def _break_the_dump(monkeypatch):
        """Make the display path's redaction of the params' dump return text that
        is not JSON."""
        import kiro_crew.agent_sdk as agent_sdk

        real = agent_sdk.tool_row_text

        def fake(text):
            return "{ not json" if text.startswith("{\n") else real(text)

        monkeypatch.setattr(agent_sdk, "tool_row_text", fake)

    def test_a_dump_that_does_not_parse_gives_no_tool_input(self, monkeypatch):
        self._break_the_dump(monkeypatch)
        assert hook_tool_input({"command": "ls", "path": "/x"}, None) is None
        # The display text is not read in its place.
        display = json.dumps({"command": "ls"})
        assert hook_tool_input({"command": "ls"}, display) is None

    @pytest.mark.asyncio
    async def test_a_dump_that_does_not_parse_fires_without_tool_input(self, probe, monkeypatch):
        store, read = probe
        self._break_the_dump(monkeypatch)
        await fire_tool_hooks(
            store,
            "Running: execute_bash",
            json.dumps({"command": "ls"}),
            raw_tool_params={"command": "ls"},
        )
        assert "tool_input" not in read()

    def test_redaction_recurses_into_nested_values_and_keeps_keys(self):
        secret = github_classic(seed=7)
        params = {
            "headers": {"X-Note": "gh key " + secret},
            "urls": [self._URL],
            "n": 3,
        }
        got = hook_tool_input(params, None)
        assert got == {
            "headers": {"X-Note": "gh key " + self._TAG},
            "urls": [self._MASKED_URL],
            "n": 3,
        }
        assert params == {
            "headers": {"X-Note": "gh key " + secret},
            "urls": [self._URL],
            "n": 3,
        }  # the caller's dict is not rewritten

    # A token the bearer pattern matches only when the header name sits beside it;
    # built from parts so no credential-shaped literal sits in the source.
    _BEARER_TOKEN = "ab12" + "-cd34" + ".ef56" + "_gh78"
    _PASSWORD = "hun" + "ter" + "2abc"

    def test_a_nested_authorization_bearer_header_is_redacted(self):
        """The bearer pattern needs the ``Authorization`` key next to the value;
        a value scanned alone would reach the hook with the raw token."""
        params = {
            "url": "https://example.com/x",
            "headers": {"Authorization": "Bearer " + self._BEARER_TOKEN},
        }
        assert self._BEARER_TOKEN not in self._display_json(params)
        got = hook_tool_input(params, None)
        assert got == {
            "url": "https://example.com/x",
            "headers": {"Authorization": "Bearer " + self._TAG},
        }
        assert params["headers"]["Authorization"] == "Bearer " + self._BEARER_TOKEN

    @pytest.mark.parametrize("command,field", [("create", "content"), ("strReplace", "newStr")])
    def test_an_authorization_header_in_json_edit_text_is_redacted(self, command, field):
        from kiro_crew.acp._dispatch_parts.surfaced_text import derive_edit_diff, redact_text

        content = json.dumps({"headers": {"Authorization": "Bearer " + self._BEARER_TOKEN}})
        params = {"command": command, "path": "/work/mcp.json", field: content}
        if command == "strReplace":
            params["oldStr"] = "{}"
        shown = redact_text(derive_edit_diff(params))
        assert self._BEARER_TOKEN not in shown
        assert self._TAG in shown
        got = hook_tool_input(params, shown)
        assert isinstance(got, dict)
        assert got["path"] == "/work/mcp.json"
        assert self._BEARER_TOKEN not in json.dumps(got)
        assert params[field] == content

    @pytest.mark.parametrize(
        "text",
        [case[1] for case in secret_cases()],
        ids=[case[0] for case in secret_cases()],
    )
    def test_raw_and_json_embedded_cases_hide_every_word_the_card_masks(self, text):
        from kiro_crew.acp._dispatch_parts.surfaced_text import derive_edit_diff, redact_text
        from kiro_crew.agent_sdk import tool_row_text

        for content in (text, json.dumps({"v": text})):
            calls = [
                {"command": "create", "path": "/work/input.json", "content": content},
                {
                    "command": "strReplace",
                    "path": "/work/input.json",
                    "oldStr": "{}",
                    "newStr": content,
                },
                {"content": content},
            ]
            for params in calls:
                shown = (
                    redact_text(derive_edit_diff(params))
                    if "command" in params
                    else tool_row_text(json.dumps(params, indent=2))
                )
                sent = json.dumps(hook_tool_input(params, shown))
                for word in text.split():
                    if len(word) >= 8 and word not in shown and json.dumps(word)[1:-1] not in shown:
                        assert word not in sent, (params, word)
                        assert json.dumps(word)[1:-1] not in sent, (params, word)

    def test_parsed_strings_and_keys_are_redacted_inside_an_off_chat_seam(self, monkeypatch):
        from kiro_crew import redaction_seams

        monkeypatch.setattr(redaction_seams, "_redaction_off", True)
        content = json.dumps({"Authorization": "Bearer " + self._BEARER_TOKEN})
        params = {"items": [{content: [content, 3, False, None]}]}
        with redaction_seams.output_seam(redaction_seams.CHAT):
            got = hook_tool_input(params, None)
        masked = json.dumps({"Authorization": "Bearer " + self._TAG})
        assert got == {"items": [{masked: [masked, 3, False, None]}]}
        assert params == {"items": [{content: [content, 3, False, None]}]}

    @pytest.mark.parametrize("name", ["password", "api_key"])
    def test_a_value_under_a_listed_credential_name_is_redacted(self, name):
        """A credential name the rule table lists is matched with its key: the
        value alone is an opaque string no row flags."""
        from kiro_crew.platform.context import redact_via_context

        assert redact_via_context(self._PASSWORD) == self._PASSWORD
        params = {name: self._PASSWORD, "region": "us-east-1"}
        assert self._PASSWORD not in self._display_json(params)
        got = hook_tool_input(params, None)
        assert got == {name: self._TAG, "region": "us-east-1"}

    def test_a_credential_used_as_a_key_is_redacted(self):
        secret = github_classic(seed=11)
        params = {secret: "x"}
        assert secret not in self._display_json(params)
        got = hook_tool_input(params, None)
        assert got == {self._TAG: "x"}

    def test_params_are_redacted_inside_a_chat_seam_with_redaction_off(self, monkeypatch):
        """The dashboard turn runs inside the chat seam, where the owner's OFF stands
        the credential pass down; a hook is no seam's audience, so it still gets masks."""
        from kiro_crew import redaction_seams

        monkeypatch.setattr(redaction_seams, "_redaction_off", True)
        secret = github_classic(seed=13)
        with redaction_seams.output_seam(redaction_seams.CHAT):
            from kiro_crew.platform.context import redact_via_context

            assert redact_via_context(secret) == secret  # the seam stands down here
            got = hook_tool_input({"command": "x " + secret, "password": self._PASSWORD}, None)
        assert got == {"command": "x " + self._TAG, "password": self._TAG}

    def test_structured_params_win_over_display_text(self):
        got = hook_tool_input({"command": "ls"}, json.dumps({"command": "other"}))
        assert got == {"command": "ls"}

    def test_without_params_the_display_text_is_parsed(self):
        assert hook_tool_input(None, json.dumps({"path": "/x"})) == {"path": "/x"}
        assert hook_tool_input(None, None) is None
        assert hook_tool_input(None, "") is None
        assert hook_tool_input(None, json.dumps(["ls"])) is None  # not an object

    def test_unparseable_display_text_is_logged_not_swallowed(self, caplog):
        caplog.set_level(logging.WARNING, logger="kiro_crew.hooks")
        assert hook_tool_input(None, self._UNPARSEABLE) is None
        logged = [
            r
            for r in caplog.records
            if r.name == "kiro_crew.hooks" and "not JSON after redaction" in r.getMessage()
        ]
        assert len(logged) == 1
        assert logged[0].exc_info is not None
        assert isinstance(logged[0].exc_info[1], json.JSONDecodeError)

    @pytest.mark.asyncio
    async def test_fire_tool_hooks_hands_the_hook_the_structured_input(self, probe):
        """End to end on the informational fire: the hook process gets
        ``tool_input.command`` from the params, with the secret value masked,
        when the display text alone would carry no ``tool_input``."""
        store, read = probe
        command = f'curl -s --url "{self._URL}"'
        await fire_tool_hooks(
            store,
            "Running: execute_bash",
            self._UNPARSEABLE,
            raw_tool_params={"command": command},
        )
        event = read()
        assert event["tool_input"] == {"command": f'curl -s --url "{self._MASKED_URL}"'}

    @pytest.mark.asyncio
    async def test_fire_tool_hooks_without_params_still_fires_and_logs(self, probe, caplog):
        caplog.set_level(logging.WARNING, logger="kiro_crew.hooks")
        store, read = probe
        await fire_tool_hooks(store, "ReadFile", self._UNPARSEABLE)
        event = read()
        assert "tool_input" not in event
        assert any("not JSON after redaction" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_permission_gate_hands_the_hook_the_structured_input(self, probe):
        """The subagent and task-runner gate builds ``tool_input`` the same way."""
        store, read = probe
        command = f'curl -s --url "{self._URL}"'
        reason = await permission_pre_tool_block(
            store,
            [],
            None,
            "Running: execute_bash",
            self._UNPARSEABLE,
            raw_tool_params={"command": command},
        )
        assert reason is None
        event = read()
        assert event["tool_input"] == {"command": f'curl -s --url "{self._MASKED_URL}"'}

    def test_every_caller_outside_the_dashboard_turn_passes_the_params(self):
        """Each production call of the two fires outside the dashboard turn hands over
        ``raw_tool_params=<event>.raw_tool_params``, so no PreToolUse fire is left
        building ``tool_input`` from the display text alone. The turn's own callers
        are driven through ``test_dashboard_approval.py`` instead of read here."""
        import re

        src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        callers = {
            "acp/client_tool_events.py": "fire_tool_hooks(",
            "llm_helpers_parts/turn_stream.py": "fire_tool_hooks(",
            "subagent_manager/run_turn.py": "fire_tool_hooks(",
            "task_executor.py": "fire_tool_hooks(",
            "tool_permission.py": "self._pre_tool(",
        }
        for rel, opener in callers.items():
            text = (src / rel).read_text(encoding="utf-8")
            calls = [m.start() for m in re.finditer(re.escape(opener), text)]
            assert calls, rel
            for start in calls:
                depth, i = 0, start + len(opener) - 1
                while True:
                    depth += {"(": 1, ")": -1}.get(text[i], 0)
                    if depth == 0:
                        break
                    i += 1
                call = text[start:i]
                assert re.search(
                    r"raw_tool_params=(\w+\.raw_tool_params|getattr\(\w+, \"raw_tool_params\", None\))",
                    call,
                ), (rel, call)


class TestScriptHookStoreFire:
    """Test ScriptHookStore.fire() emits subagent_id, parent_session_key, agent_role into hook_event.

    These tests register a real hook in the store, patch run_script_hook to capture
    the hook_event payload, and assert that the conditional emission branches in fire()
    add (or omit) the new fields correctly.
    """

    @pytest.fixture
    def fire_store(self, tmp_path: Path) -> ScriptHookStore:
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "test-hook",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "matcher": "",
            "command": "echo test",
        })
        return store

    @pytest.mark.asyncio
    async def test_fire_emits_subagent_id_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                subagent_id="sub-abc",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["subagent_id"] == "sub-abc"
            assert "parent_session_key" not in hook_event
            assert "agent_role" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_parent_session_key_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                parent_session_key="dashboard:slot-1",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["parent_session_key"] == "dashboard:slot-1"
            assert "subagent_id" not in hook_event
            assert "agent_role" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_session_key_when_set(self, fire_store: ScriptHookStore):
        """``session_key`` is the firing session's own key: stamped as itself, and
        never spelled as ``parent_session_key`` (a subagent's parent pointer) or
        as ``subagent_id``, so a hook keyed on those cannot mistake a top-level
        turn for a spawned subagent's."""
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                session_key="dashboard:slot-1",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["session_key"] == "dashboard:slot-1"
            assert "parent_session_key" not in hook_event
            assert "subagent_id" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_omits_session_key_when_unset(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="ReadFile")
            (_, _, hook_event), _ = mock_run.call_args
            assert "session_key" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_agent_role_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                agent_role="utility",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["agent_role"] == "utility"
            assert "subagent_id" not in hook_event
            assert "parent_session_key" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_all_three_together(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                subagent_id="sub-abc",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["subagent_id"] == "sub-abc"
            assert hook_event["parent_session_key"] == "dashboard:slot-1"
            assert hook_event["agent_role"] == "utility"

    @pytest.mark.asyncio
    async def test_fire_omits_all_three_when_none(self, fire_store: ScriptHookStore):
        """Backward compatibility: when all three are None (default), payload is byte-identical to pre-CR behavior."""
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="ReadFile")
            (_, _, hook_event), _ = mock_run.call_args
            assert "subagent_id" not in hook_event
            assert "parent_session_key" not in hook_event
            assert "agent_role" not in hook_event


class TestScriptHookStoreStopContext:
    """Stop hooks receive the final assistant segment on stdin, untruncated.

    The env var ``KIROCREW_HOOK_CONTEXT`` is capped at 500 chars (ARG_MAX
    safety), which drops the tail of the segment. A Stop hook that keys on tail
    content (e.g. the harness ``[OPTIONS:]`` menu line) never sees it via the env
    var. fire() therefore emits the untruncated segment into the stdin
    ``hook_event`` payload as ``assistant_text`` — mirroring the existing
    ``prompt`` key for UserPromptSubmit, but on a dedicated arg so the env value
    can stay bounded.
    """

    @pytest.fixture
    def stop_store(self, tmp_path: Path) -> ScriptHookStore:
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "stop-hook",
            "event": HOOK_EVENT_STOP,
            "matcher": "",
            "command": "echo test",
        })
        return store

    @pytest.mark.asyncio
    async def test_stop_full_context_on_stdin_and_matcher(self, stop_store: ScriptHookStore):
        # The load-bearing marker sits at the tail, past the 500-char env cap.
        full = ("x" * 900) + "\n[OPTIONS: A | B | C]"
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "stop-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await stop_store.fire(HOOK_EVENT_STOP, context=full)
            (_, ctx_arg, hook_event), _ = mock_run.call_args
            # stdin payload carries the FULL segment, tail marker intact.
            assert hook_event["assistant_text"] == full
            assert "[OPTIONS:" in hook_event["assistant_text"]
            # fire() passes the FULL context downstream (matcher + env source);
            # the env-only 500-cap lives in run_script_hook, not here — a tail
            # matcher must therefore still be able to see the marker.
            assert ctx_arg == full

    @pytest.mark.asyncio
    async def test_stop_tail_matcher_is_not_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_script: Path
    ):
        # A Stop hook whose matcher targets tail content must still fire — fire()
        # matches against the full context, not the 500-char env slice.
        command, read = _probe_command(tmp_path, monkeypatch, probe_script)
        store = ScriptHookStore(tmp_path / "store")
        store.create({
            "name": "options-stop-hook",
            "event": HOOK_EVENT_STOP,
            "matcher": "*[OPTIONS:*",
            "command": command,
        })
        full = ("x" * 900) + "\n[OPTIONS: A | B | C]"
        results = await store.fire(HOOK_EVENT_STOP, context=full)
        assert [r.exit_code for r in results] == [0], (
            "tail-matching Stop hook was filtered out by env truncation"
        )
        assert read()["assistant_text"] == full

    @pytest.mark.asyncio
    async def test_stop_empty_turn_still_emits_key(self, stop_store: ScriptHookStore):
        # An empty / no-output turn still fires Stop with context="". The key MUST
        # be present (unconditional, not truthiness-gated) so a hook that always
        # reads hook_event["assistant_text"] gets "" rather than KeyError-ing.
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "stop-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await stop_store.fire(HOOK_EVENT_STOP, context="")
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["assistant_text"] == ""

    @pytest.mark.asyncio
    async def test_user_prompt_submit_still_uses_prompt_key(self, tmp_path: Path):
        # Regression guard: the Stop change must not bleed into UPS, which keeps
        # delivering its full context under the existing ``prompt`` key.
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "ups-hook",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "matcher": "",
            "command": "echo test",
        })
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "ups-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, context="hello")
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["prompt"] == "hello"
            assert "assistant_text" not in hook_event


class TestRunScriptHookStopEnvCap:
    """run_script_hook caps the Stop env var at 500 chars while the full
    segment still reaches the hook via stdin JSON (ARG_MAX safety)."""

    @pytest.mark.asyncio
    async def test_stop_env_context_capped_but_stdin_full(self) -> None:
        """Stop hook: KIROCREW_HOOK_CONTEXT env is capped at 500; stdin JSON is full.

        The env var is bounded by ARG_MAX (a multi-KB turn there can fail process
        creation), so run_script_hook truncates the ENV copy for Stop only — while
        the full segment still reaches the hook via the stdin ``assistant_text``
        payload that fire() built. Captures both channels off a mocked subprocess.
        """
        full = ("x" * 900) + "\n[OPTIONS: A | B | C]"
        hook = ScriptHook(id="s1", name="stop-hook", event=HOOK_EVENT_STOP, command="cat", timeout=5)
        hook_event = {"hook_event_name": HOOK_EVENT_STOP, "cwd": "/", "assistant_text": full}

        class FakeStdin:
            def __init__(self):
                self.data = bytearray()
                self.closed = False

            def write(self, data):
                self.data.extend(data)

            async def drain(self):
                return None

            def close(self):
                self.closed = True

        fake_proc = MagicMock()
        fake_proc.stdin = FakeStdin()
        fake_proc.stdout = asyncio.StreamReader()
        fake_proc.stdout.feed_eof()
        fake_proc.stderr = asyncio.StreamReader()
        fake_proc.stderr.feed_eof()
        fake_proc.wait = AsyncMock(return_value=0)
        fake_proc.returncode = 0
        captured: dict = {}

        async def fake_exec(*argv, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return fake_proc

        # Both spawn forms are patched because the choice is platform-dependent:
        # Windows hands the command line to ``create_subprocess_shell`` so
        # cmd.exe parses the operator's quotes verbatim, POSIX execs
        # ``/bin/sh -c`` as an argv. The env cap under test is identical either
        # way, so the test must not assume one host's form.
        with (
            patch("kiro_crew.sandbox.wrap_argv", lambda argv, *a, **k: (argv, None)),
            patch("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("asyncio.create_subprocess_shell", side_effect=fake_exec),
        ):
            await run_script_hook(hook, context=full, hook_event=hook_event)

        # ENV copy is capped at 500 chars — the tail marker is dropped there.
        env_ctx = captured["env"]["KIROCREW_HOOK_CONTEXT"]
        assert len(env_ctx) == 500
        assert "[OPTIONS:" not in env_ctx
        # Assert on what actually reached the subprocess stdin (not the input
        # dict): the full segment, tail marker intact, is serialized to stdin.
        stdin_bytes = bytes(fake_proc.stdin.data)
        assert fake_proc.stdin.closed is True
        parsed = json.loads(stdin_bytes)
        assert parsed["assistant_text"] == full
        assert "[OPTIONS:" in parsed["assistant_text"]


class TestRunScriptHookGovernanceScopeKey:
    """run_script_hook resolves the governance scope from the payload's session
    fields: a subagent's event names the spawning session (``parent_session_key``),
    a top-level turn names itself (``session_key``). Asserted through the denial
    path so no subprocess is spawned."""

    @staticmethod
    async def _scope_seen(hook_event: dict) -> str:
        seen: list[str] = []

        def _deny(sk: str = "") -> str | None:
            seen.append(sk)
            return "off"

        hook = ScriptHook(id="g1", name="gov-hook", event=HOOK_EVENT_PRE_TOOL_USE, command="cat", timeout=5)
        with patch("kiro_crew.hooks._script_hooks_capability_denied", _deny):
            result = await run_script_hook(hook, hook_event=hook_event)
        assert result.exit_code == 2
        assert seen, "the gate was not consulted"
        return seen[0]

    @pytest.mark.asyncio
    async def test_top_level_turn_scopes_by_its_own_session_key(self) -> None:
        sk = await self._scope_seen(
            {"hook_event_name": HOOK_EVENT_PRE_TOOL_USE, "cwd": "/", "session_key": "dashboard:s1"}
        )
        assert sk == "dashboard:s1"

    @pytest.mark.asyncio
    async def test_subagent_event_scopes_by_parent(self) -> None:
        sk = await self._scope_seen(
            {
                "hook_event_name": HOOK_EVENT_PRE_TOOL_USE,
                "cwd": "/",
                "subagent_id": "sub-1",
                "parent_session_key": "dashboard:s1",
                "session_key": "subagent:sub-1",
            }
        )
        assert sk == "dashboard:s1"
