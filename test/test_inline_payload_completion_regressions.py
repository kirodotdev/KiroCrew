"""Base64 argument regressions; fixtures are text, never executed as programs."""

from __future__ import annotations

import ast
import base64
import inspect
import shlex

import pytest

from kiro_crew.security import argv_floor, inline_payload


def _encode(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class TestDecoderArguments:
    @pytest.mark.parametrize(
        "decoder", ["b64decode", "standard_b64decode", "urlsafe_b64decode", "decodebytes"]
    )
    def test_keyword_input(self, decoder):
        payload = f'base64.{decoder}(s=b"{_encode("hello")}")'
        assert inline_payload._decode_call_literals(payload) == ("hello",)

    @pytest.mark.parametrize(
        "arguments",
        [
            's="{value}", validate=True',
            'validate=True, s="{value}"',
            's="{value}", altchars=b"-_"',
            'altchars=b"-_", s="{value}", validate=True',
            '"{value}", None, True',
        ],
    )
    def test_reordered_and_positional_options(self, arguments):
        payload = "base64.b64decode(" + arguments.format(value=_encode("hello")) + ")"
        assert inline_payload._decode_call_literals(payload) == ("hello",)

    @pytest.mark.parametrize("module", ["base64", "b64"])
    def test_nested_keyword_input_keeps_raw_case(self, module):
        inner = _encode("import kiro_crew.cli")
        outer = _encode(inner)
        payload = (
            f"{module}.b64decode(validate=True, s=" f'{module}.standard_b64decode(s="{outer}"))'
        )
        assert inline_payload._decode_call_literals(payload) == (
            inner.lower(),
            "import kiro_crew.cli",
        )

    @pytest.mark.parametrize(
        "option",
        [
            "dict(a=1, b=2)",
            "['(', ')', ',']",
            "{'comma': ',', 'paren': ')'}",
            "'''a ) , ' string'''",
        ],
    )
    def test_nested_option_cannot_split_input_argument(self, option):
        # These are syntax probes, not claims that each option is valid base64 API input.
        payload = f'base64.b64decode(altchars={option}, s="{_encode("hello")}")'
        assert inline_payload._decode_call_literals(payload) == ("hello",)

    def test_comments_cannot_split_input_argument(self):
        payload = f'base64.b64decode(validate=True, # ), s=\n s="{_encode("hello")}")'
        assert inline_payload._decode_call_literals(payload) == ("hello",)

    def test_unrelated_keyword_data_is_not_decoded(self):
        payload = f'base64.b64decode(s=unknown, altchars="{_encode("kirocrew token")}")'
        assert inline_payload._decode_call_literals(payload) == ()

    def test_data_literals_do_not_consume_the_decode_budget(self, monkeypatch):
        calls = []
        decode = inline_payload.base64.b64decode

        def counted(value, **kwargs):
            calls.append(value)
            return decode(value, **kwargs)

        monkeypatch.setattr(inline_payload.base64, "b64decode", counted)
        data = ",".join(repr(_encode("kirocrew token")) for _ in range(200))
        payload = f'data=[{data}]; base64.b64decode(s="{_encode("hello")}")'
        assert inline_payload._decode_call_literals(payload) == ("hello",)
        assert len(calls) == 1

    def test_all_actual_calls_are_decoded_without_a_count_cap(self):
        payload = ";".join(f'base64.b64decode(s="{_encode("hello")}")' for _ in range(200))
        assert inline_payload._decode_call_literals(payload) == ("hello",) * 200


class TestCallerPreservation:
    @pytest.mark.parametrize("attached", [False, True])
    @pytest.mark.parametrize("nested", [False, True])
    def test_keyword_encoded_execution_remains_denied(self, attached, nested):
        encoded = _encode("import kiro_crew.cli")
        expression = f'base64.b64decode(s="{encoded}")'
        if nested:
            expression = (
                "base64.b64decode(validate=True, s="
                f'base64.urlsafe_b64decode(s="{_encode(encoded)}"))'
            )
        command = "python -c" + ("" if attached else " ") + shlex.quote(f"exec({expression})")
        assert argv_floor._is_credential_mint(command.lower(), raw_text=command)

    def test_hello_and_unrelated_encoded_fixture_remain_allowed(self):
        payload = (
            f'print(base64.b64decode(s="{_encode("hello")}")); '
            f'print(len("{_encode("kirocrew token")}"))'
        )
        command = "python -c " + shlex.quote(payload)
        assert not argv_floor._is_credential_mint(command.lower(), raw_text=command)

    def test_printed_decoder_syntax_remains_data(self):
        payload = repr(f'base64.b64decode(s="{_encode("kirocrew token")}")')
        command = "python -c " + shlex.quote(f"print({payload})")
        assert not argv_floor._is_credential_mint(command.lower(), raw_text=command)

    def test_names_the_mint_stays_nonrecursive(self):
        tree = ast.parse(inspect.getsource(inline_payload._names_the_mint))
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_names_the_mint"
            for node in ast.walk(tree)
        )
        payload = "exec(" * 1200 + "'patch.py'" + ")" * 1200
        assert not inline_payload._inline_payload_reaches_cli(payload)

    def test_compile_changed_module_without_writing_bytecode(self):
        compile(inspect.getsource(inline_payload), inline_payload.__file__, "exec")


class TestRawPayloadOwnership:
    @pytest.mark.parametrize("carrier", ["shell", "pipe", "heredoc", "here-string"])
    def test_encoded_execution_on_each_carrier(self, carrier):
        payload = f'exec(base64.b64decode(s="{_encode("import kiro_crew.cli")}"))'
        quoted = shlex.quote(payload)
        commands = {
            "shell": "bash -c " + shlex.quote("python -c " + quoted),
            "pipe": "printf %s " + quoted + " | python -",
            "heredoc": "python - <<'PY'\n" + payload + "\nPY",
            "here-string": "python - <<< " + quoted,
        }
        command = commands[carrier]
        assert argv_floor._is_credential_mint(command.lower(), raw_text=command)

    def test_another_commands_decoder_cannot_contaminate_hello(self):
        hello = "python -c " + shlex.quote('print(base64.b64decode(s="aGVsbG8="))')
        # The second command prints Python syntax; it never executes that syntax.
        other = "printf %s " + shlex.quote(
            f'base64.b64decode(s="{_encode("import kiro_crew.cli")}")'
        )
        command = hello + "; " + other
        assert not argv_floor._is_credential_mint(command.lower(), raw_text=command)

    def test_imported_decoder_alias_reaches_the_same_input(self):
        payload = (
            "from base64 import b64decode as decode; "
            f'exec(decode(s="{_encode("import kiro_crew.cli")}"))'
        )
        command = "python -c " + shlex.quote(payload)
        assert argv_floor._is_credential_mint(command.lower(), raw_text=command)


class TestExplicitDecoderAliases:
    @pytest.mark.parametrize(
        "decoder", ["b64decode", "standard_b64decode", "urlsafe_b64decode", "decodebytes"]
    )
    def test_explicit_alias_is_scoped_to_its_payload(self, decoder):
        payload = (
            f"from base64 import {decoder} as decode; "
            f'exec(decode(s=b"{_encode("import kiro_crew.cli")}"))'
        )
        assert inline_payload._decode_call_literals(payload) == ("import kiro_crew.cli",)
        command = "python -c " + shlex.quote(payload)
        assert argv_floor._is_credential_mint(command.lower(), raw_text=command)

    @pytest.mark.parametrize("parentheses", [False, True])
    def test_nested_aliases_and_import_lists(self, parentheses):
        imports = "b64decode as outer, standard_b64decode as inner"
        if parentheses:
            imports = "(\n" + imports + ", # import comment\n)"
        encoded = _encode("import kiro_crew.cli")
        payload = f'from base64 import {imports}; exec(outer(s=inner(s=b"{_encode(encoded)}")))'
        assert inline_payload._decode_call_literals(payload) == (
            encoded.lower(),
            "import kiro_crew.cli",
        )

    @pytest.mark.parametrize(
        "prefix",
        [
            "def decode(s): return s\n",
            "from other import decode; ",
            "print('from base64 import b64decode as decode'); ",
            "# from base64 import b64decode as decode\n",
            "from base64 import b64encode as decode; ",
        ],
    )
    def test_custom_or_printed_import_is_not_a_decoder(self, prefix):
        payload = prefix + f'print(decode(s=b"{_encode("kirocrew token")}"))'
        assert inline_payload._decode_call_literals(payload) == ()
        command = "python -c " + shlex.quote(payload)
        assert not argv_floor._is_credential_mint(command.lower(), raw_text=command)

    def test_an_imported_alias_does_not_name_an_objects_method(self):
        payload = (
            "from base64 import b64decode as decode; "
            f'obj.decode(s=b"{_encode("kirocrew token")}")'
        )
        assert inline_payload._decode_call_literals(payload) == ()

    def test_hello_with_unrelated_fixture_is_allowed(self):
        payload = (
            "from base64 import b64decode as decode; "
            f'print(decode(s=b"{_encode("hello")}")); data="{_encode("kirocrew token")}"'
        )
        assert inline_payload._decode_call_literals(payload) == ("hello",)
        command = "python -c " + shlex.quote(payload)
        assert not argv_floor._is_credential_mint(command.lower(), raw_text=command)

    def test_aliases_do_not_leak_between_python_commands(self):
        first = 'from base64 import b64decode as decode; print(decode(s=b"aGVsbG8="))'
        second = f'def decode(s): return s\nprint(decode(s=b"{_encode("kirocrew token")}"))'
        command = "python -c " + shlex.quote(first) + "; python -c " + shlex.quote(second)
        assert inline_payload._decoded_b64_literals(command) == ("hello",)
        assert not argv_floor._is_credential_mint(command.lower(), raw_text=command)

    def test_import_scan_runs_once_for_many_calls(self, monkeypatch):
        scans = []
        original = inline_payload._decoder_call_names

        def record(view):
            scans.append(len(view))
            return original(view)

        monkeypatch.setattr(inline_payload, "_decoder_call_names", record)
        payload = "from base64 import b64decode as decode; " + ";".join(
            'decode(s=b"aGVsbG8=")' for _ in range(200)
        )
        assert inline_payload._decode_call_literals(payload) == ("hello",) * 200
        # Once, over the folded view: the fold normalises each ``b"..."`` to a plain
        # literal, so the text that is scanned is the folded view, not the payload.
        assert scans == [len(inline_payload._fold_inline_literals(payload))]
