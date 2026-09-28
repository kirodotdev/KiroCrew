"""Scoped inline mint checks over inert strings; no fixture program is executed."""

from __future__ import annotations

import ast
import base64
import inspect
import re
import shlex

import pytest

from kiro_crew import security
from kiro_crew.security import argv_floor, inline_payload

_MINT = "credential-exfil-kirocrew-token"
_ENCODED_MINT = "a2lyb2NyZXcgdG9rZW4="


def _rule_of(cmd: str) -> str | None:
    reason = security.is_denied(cmd)
    if reason is None:
        return None
    m = re.search(r"rule=(\S+)", reason)
    return m.group(1) if m else reason.splitlines()[0]


class TestAnEncodedLiteralIsDataUntilSomethingDecodesIt:
    @pytest.mark.parametrize(
        "payload",
        [
            f'print(len("{_ENCODED_MINT}"))',
            f'print("{_ENCODED_MINT}" in open("test/test_argv_floor.py").read())',
            f'fixtures = ["{_ENCODED_MINT}"]; print(len(fixtures))',
            f'import base64; print(base64.b64decode("aGVsbG8=")); print(len("{_ENCODED_MINT}"))',
            f'print("b64decode"); print(len("{_ENCODED_MINT}"))',
            f"print(\"b64decode('{_ENCODED_MINT}')\")",
        ],
    )
    def test_a_carried_literal_is_not_the_programs_own_text(self, payload):
        # shlex.quote is shell quoting only, never execution of the fixture.
        cmd = "python -c " + shlex.quote(payload)
        assert not argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) is None, cmd

    @pytest.mark.parametrize(
        "decoder", ["b64decode", "standard_b64decode", "urlsafe_b64decode", "decodebytes"]
    )
    def test_a_decoded_literal_is_still_read_when_a_decoder_runs(self, decoder):
        cmd = f"python -c 'import os,base64; os.system(base64.{decoder}(b\"{_ENCODED_MINT}\").decode())'"
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) == _MINT, cmd

    def test_only_decoder_arguments_are_decoded(self):
        payload = f'print(base64.b64decode("aGVsbG8=")); print("{_ENCODED_MINT}")'
        assert inline_payload._decoded_b64_literals(payload) == ("hello",)
        assert inline_payload._decoded_b64_literals(f'x = "{_ENCODED_MINT}"') == ()
        assert (
            inline_payload._decoded_b64_literals(f"print(\"b64decode('{_ENCODED_MINT}')\")") == ()
        )

    def test_nested_decoding_preserves_case_until_resolved(self):
        inner = base64.b64encode(b"import kiro_crew.cli").decode()
        outer = base64.b64encode(inner.encode()).decode()
        cmd = f"python -c 'exec(base64.b64decode(base64.b64decode(\"{outer}\")))'"
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd)
        assert _rule_of(cmd) == _MINT
        assert inline_payload._decoded_b64_literals(cmd) == (inner.lower(), "import kiro_crew.cli")

    def test_many_data_literals_do_not_spend_decode_work(self, monkeypatch):
        calls = 0
        real = inline_payload.base64.b64decode

        def count(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(inline_payload.base64, "b64decode", count)
        data = ", ".join(repr(_ENCODED_MINT) for _ in range(200))
        payload = f'x = [{data}]; print(base64.b64decode("aGVsbG8="))'
        assert inline_payload._decoded_b64_literals(payload) == ("hello",)
        assert calls == 1


class TestTheConsoleScriptIsAWholePathComponent:
    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -c \"import runpy; runpy.run_path('/w/kirocrew-scratch/kirocrew.py')\"",
            "py -3 -c \"import runpy; runpy.run_path(r'C:\\\\w\\\\kirocrew-scratch\\\\kirocrew.py')\"",
            "python3 -c \"exec(open('/w/kirocrew-scratch/kirocrew.py').read())\"",
            "python3 -c \"import runpy; runpy.run_path('/w/scratch/kirocrew.pyc')\"",
            "python3 -c \"exec(open('/w/scratch/kiro-crew.py').read())\"",
        ],
    )
    def test_a_user_file_named_for_the_product_is_not_the_program(self, cmd):
        assert not argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -c \"exec(open(shutil.which('kirocrew')).read())\"",
            "python3 -c \"exec(open('/opt/venv/bin/kirocrew').read())\"",
            "python3 -c \"exec(open(r'C:\\\\venv\\\\Scripts\\\\kirocrew.exe').read())\"",
            "python3 -c \"import runpy,shutil; runpy.run_path(shutil.which('kiro-crew'))\"",
        ],
    )
    def test_the_installed_entry_point_is_still_the_program(self, cmd):
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) == _MINT, cmd

    def test_the_boundary_is_the_component_not_a_prefix(self):
        search = inline_payload._CONSOLE_SCRIPT_LITERAL_RE.search
        for program in ("'kirocrew'", "'/venv/bin/kirocrew'", "kirocrew.exe'"):
            assert search(program), program
        for other in (
            "'kirocrew.py'",
            "'kirocrew.pyc'",
            "'/w/kirocrew/x.py'",
            "kirocrew_ws",
            "x.kirocrew",
        ):
            assert not search(other), other


class TestACallArgumentHasOneQuoteAwareBoundary:
    @pytest.mark.parametrize(
        "payload",
        [
            "exec(open([')', shutil.which('kirocrew')][1]).read())",
            "runpy.run_path(run_name=str(')'), path_name=shutil.which('kirocrew'))",
            "exec(open( # ) a comment\n shutil.which('kirocrew')).read())",
            "exec(open(['''single ' and )''', shutil.which('kirocrew')][1]).read())",
            "exec(open(['a\\')', shutil.which('kirocrew')][1]).read())",
        ],
    )
    def test_a_quoted_paren_cannot_hide_the_program(self, payload):
        assert inline_payload._inline_payload_reaches_cli(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            "exec('print(\"(\")'); print('kirocrew done')",
            "exec(open('a(b').read()); x = 'kirocrew'",
            "exec('''print(\"(\")'''); print('kirocrew done')",
            "exec(open('patch.py').read()); print('from kiro_crew.acp import x, secret hint')",
        ],
    )
    def test_later_prose_is_not_a_loader_argument(self, payload):
        assert not inline_payload._inline_payload_reaches_cli(payload)

    def test_an_unterminated_call_is_still_judged_on_everything_left(self):
        for payload in ("exec(" + "f(" * 200 + "'kirocrew'", "exec(" + "x" * 20000 + "'kirocrew'"):
            assert inline_payload._inline_payload_reaches_cli(payload), payload[:32]


class TestACallIsJudgedByWhatItResolvesTo:
    """A callable reached by any static binding is read as the callable it is."""

    # `import <pkg>.cli`, encoded. Inert text: nothing here decodes it but the gate.
    ENCODED = "aW1wb3J0IGtpcm9fY3Jldy5jbGk="

    @pytest.mark.parametrize(
        "spelling",
        [
            # The name written at the call, and reached through a module alias.
            'import runpy; runpy.run_module("kiro_crew")',
            'import runpy as rp; rp.run_module("kiro_crew")',
            # Bound by a from-import, with and without an alias.
            'from runpy import run_module as r; r("kiro_crew")',
            'from runpy import run_module; run_module("kiro_crew")',
            'from importlib import import_module as m; m("kiro_crew")',
            # Bound by assignment, from the module or from a resolving head.
            'import runpy; r = runpy.run_module; r("kiro_crew")',
            'import runpy; r = getattr(runpy, "run_module"); r("kiro_crew")',
            'import runpy; r = runpy.__dict__["run_module"]; r("kiro_crew")',
            # Called straight off the head, with no name to read at all.
            'import runpy; getattr(runpy, "run_module")("kiro_crew")',
            'import runpy; runpy.__dict__["run_module"]("kiro_crew")',
            # The bound name spelled in pieces, which the fold joins first.
            'import runpy; getattr(runpy, "run_" "module")("kiro_crew")',
            # Grouped: the parentheses are not an operation, so the head is what
            # they wrap -- dotted, nested, spaced, or a bound name.
            'import runpy; (runpy.run_module)("kiro_crew")',
            'import runpy; ((runpy.run_module))("kiro_crew")',
            'import runpy; ( runpy.run_module )("kiro_crew")',
            'from runpy import run_module as r; (r)("kiro_crew")',
            'import runpy; r = (runpy.run_module); r("kiro_crew")',
        ],
    )
    def test_every_static_binding_reaches_the_runner(self, spelling):
        cmd = "python3 -c " + shlex.quote(spelling)
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) == _MINT, cmd

    @pytest.mark.parametrize(
        "spelling",
        [
            # The loader gate reads the same table: each of these loads the
            # installed console script, whose dispatch is the mint.
            'import shutil; exec(open(shutil.which("kirocrew")).read())',
            'from builtins import exec as e; import shutil; e(open(shutil.which("kirocrew")).read())',
            'import builtins,shutil; getattr(builtins, "exec")(open(shutil.which("kirocrew")).read())',
            'import builtins,shutil; g = builtins.__dict__["exec"]; g(open(shutil.which("kirocrew")).read())',
            'import builtins,shutil; (builtins.exec)(open(shutil.which("kirocrew")).read())',
        ],
    )
    def test_every_static_binding_reaches_the_loader(self, spelling):
        cmd = "python3 -c " + shlex.quote(spelling)
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) == _MINT, cmd

    @pytest.mark.parametrize(
        "binding",
        [
            "from base64 import b64decode as d",
            "import base64; d = base64.b64decode",
            'import base64; d = getattr(base64, "b64decode")',
            'import base64; d = base64.__dict__["b64decode"]',
        ],
    )
    def test_every_static_binding_reaches_the_decoder(self, binding):
        # One table, so a spelling closed for the runner is closed for the decoder:
        # the encoded body is only read because something in the payload decodes it.
        spelling = '%s; exec(d("%s"))' % (binding, self.ENCODED)
        cmd = "python3 -c " + shlex.quote(spelling)
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) == _MINT, cmd

    @pytest.mark.parametrize(
        "spelling",
        [
            # A resolved callable is judged by what it is HANDED, exactly as the
            # written name is: another file, or a path under the package, is not
            # the package.
            'import runpy; runpy.run_path("patch.py")',
            'from runpy import run_path as rp; rp("patch.py")',
            'from runpy import run_path as rp; rp("src/kiro_crew/x.py")',
            'import runpy; getattr(runpy, "run_path")("src/kiro_crew/x.py")',
            # A binding to anything outside the vocabulary stays generic code.
            'import json; print(getattr(json, "dumps")({}))',
            'import os; print(os.__dict__["getcwd"](), "src/kiro_crew")',
            # A same-named callable from an unrelated module is not the stdlib one.
            'from other import b64decode as d; print(d("aGVsbG8="))',
            # An imported alias is a bare name, so an object attribute is not it.
            'from base64 import b64decode as d; print(obj.d("aW1wb3J0IGtpcm9fY3Jldy5jbGk="))',
            # A binding written inside a string binds nothing.
            'print("r = run_module"); print("kiro_crew")',
            # A GROUPED head is judged by what it is handed, like any other.
            'import runpy; (runpy.run_path)("patch.py")',
            'import runpy; (runpy.run_path)("src/kiro_crew/x.py")',
            "import json; print((json.dumps)({}))",
            # A call's own argument list is not a grouping: this hands over what
            # partial RETURNS, not the callable named inside it.
            'import functools; print(functools.partial(print)("kiro_crew"))',
            # ... and a plain tuple is data, not a head.
            'print(("kiro_crew", "x"))',
        ],
    )
    def test_a_binding_to_anything_else_is_not_a_mint(self, spelling):
        cmd = "python3 -c " + shlex.quote(spelling)
        assert not argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) is None, cmd

    def test_a_head_resolves_only_from_its_own_name_argument(self):
        # A runner name in `getattr`'s DEFAULT slot is not what the call resolves to.
        assert not inline_payload._inline_payload_reaches_cli(
            'getattr(obj, "dumps", "run_module")("kiro_crew")'
        )
        # ... a one-argument call resolves to nothing ...
        assert not inline_payload._inline_payload_reaches_cli('getattr(runpy)("kiro_crew")')
        # ... and a subscript by a computed key is the noted residual, not a reach.
        assert not inline_payload._inline_payload_reaches_cli('runpy.__dict__[name]("kiro_crew")')

    def test_a_bound_name_is_the_callable_and_not_its_result(self):
        table = inline_payload._callable_aliases(
            "r = runpy.run_module", inline_payload._call_spans(""), {}
        )
        assert table == {"r": "run_module"}
        # Calling it binds the RESULT, which is not a callable this gate reads.
        called = "r = runpy.run_module('x')"
        assert (
            inline_payload._callable_aliases(
                called, inline_payload._call_spans(called), inline_payload._argument_spans(called)
            )
            == {}
        )


class TestTheInspectionIsOnePassAndNotRecursive:
    def test_names_the_mint_does_not_call_itself(self):
        tree = ast.parse(inspect.getsource(inline_payload._names_the_mint))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_names_the_mint" not in called

    def test_nesting_is_still_read_without_the_rescan(self):
        assert inline_payload._inline_payload_reaches_cli(
            "exec(compile('import kiro_crew.config.loader as l; l.read_local_secret(1)', 'x', 'exec'))"
        )
        assert not inline_payload._inline_payload_reaches_cli(
            "exec(compile('from kiro_crew.acp import client', 'x', 'exec'))"
        )

    def test_nested_loaders_do_not_copy_or_scan_each_tail(self, monkeypatch):
        scanned = 0
        real = inline_payload._call_spans

        def count(view):
            nonlocal scanned
            scanned += len(view)
            return real(view)

        monkeypatch.setattr(inline_payload, "_call_spans", count)
        payload = "exec(" * 1200 + "'p.py'" + ")" * 1200
        assert not inline_payload._inline_payload_reaches_cli(payload)
        assert scanned <= 3 * len(payload)
        args = inline_payload._code_loader_arguments(payload)
        assert len(args) == 1
        assert sum(map(len, args)) <= len(payload)


class TestTheGenuineProtectionsSurvive:
    @pytest.mark.parametrize(
        "cmd",
        [
            "python -c 'from kiro_crew.cli import main; main()'",
            "python -c 'from kiro_crew.cli_server import _token; _token(None)'",
            "python -c 'from kiro_crew.config.loader import read_local_secret; print(read_local_secret(5476))'",
            "python -c 'from kiro_crew.instances import run_marker; print(run_marker.read_secret(5476))'",
            "python3 -c \"import runpy; runpy.run_module(mod_name='kiro_crew', run_name='__main__')\"",
            "python -c \"exec('from kiro_crew.config.loader import read_local_secret; f()')\"",
            "python3 - <<'PY'\nfrom kiro_crew.dashboard.token_auth import generate_token\nPY",
        ],
    )
    def test_the_mint_surface_is_still_denied(self, cmd):
        assert _rule_of(cmd) == _MINT, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -c \"import ast; ast.parse(open('src/kiro_crew/security.py').read())\"",
            "python3 -c 'from kiro_crew.acp import client; print(client)'",
            "python3 -c \"exec(open('patch.py').read())\"",
            "python3 -c \"print('kiro_crew docs mention the token verb')\"",
        ],
    )
    def test_an_ordinary_mention_is_still_allowed(self, cmd):
        assert _rule_of(cmd) is None, cmd


_READER = "from kiro_crew.config.loader import read_local_secret"


class TestAnImportBeginsAtEveryStatementBoundary:
    """The product-import anchor reads every place Python lets a statement start.

    A simple statement begins at the start of input, after ``;``, after a newline,
    or after the ``:`` that closes a compound header. Those four are the whole set
    the grammar has, so the anchor is closed by construction: a ``from kiro_crew``
    tucked behind ``if True:`` is the same import as one on its own line.
    """

    @pytest.mark.parametrize(
        "prefix",
        [
            "",
            "x = 1; ",
            "\n",
            "if True: ",
            "for _ in [0]: ",
            "while 1: ",
            "try: ",
            "with open('/dev/null'): ",
            "def f(): ",
            "class C: ",
            "if True:\n    ",
            "x = 1\nif x: ",
        ],
    )
    def test_every_boundary_the_grammar_has_reaches_the_import(self, prefix):
        payload = f"{prefix}{_READER}; print(read_local_secret())"
        assert _rule_of(f'python -c "{payload}"') == _MINT, repr(prefix)

    def test_a_colon_inside_an_ordinary_expression_starts_nothing(self):
        """Dict, slice and annotation colons are not statement boundaries.

        None of these is followed by an import, so the wider class must not turn a
        plain colon into a reason: the anchor still needs the import itself.
        """
        for payload in (
            "d = {'kiro_crew': 1}; print(d)",
            "s = 'kiro_crew token'[0:4]; print(s)",
            "x: int = 1; print('kiro_crew', x)",
            "print({'a': 'from kiro_crew.acp import client'})",
        ):
            assert _rule_of(f'python -c "{payload}"') is None, payload


class TestAPackagePathIsOneClassOfSeparators:
    """The mint surface reads a package path however its separators are spelled.

    ``kiro_crew.cli`` is a module, ``kiro_crew/cli.py`` a POSIX path and
    ``kiro_crew\\cli.py`` a Windows path; all three name the same file, so the
    matcher treats the three separators as one class. The Windows spellings
    below are raw or doubled, which is what a payload that actually opens the
    file on Windows has to write.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            "exec(open(r'src\\kiro_crew\\cli.py').read())",
            "exec(open('src\\\\kiro_crew\\\\cli.py').read())",
            "import runpy; runpy.run_path(r'src\\kiro_crew\\__main__.py')",
            "exec(open(r'src\\kiro_crew\\dashboard\\token_auth.py').read())",
        ],
    )
    def test_a_windows_path_to_the_mint_is_still_the_mint(self, payload):
        assert _rule_of(f'python -c "{payload}"') == _MINT, payload

    @pytest.mark.parametrize(
        "cmd",
        [
            r"isort src\kiro_crew\mcp_core.py",
            "python -c \"print(r'src\\kiro_crew\\acp\\client.py')\"",
            "python -c \"import ast; ast.parse(open(r'src\\kiro_crew\\security.py').read())\"",
        ],
    )
    def test_an_ordinary_windows_path_is_still_allowed(self, cmd):
        assert _rule_of(cmd) is None, cmd


def _split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class TestTheFoldIsClosedOverConstantExpressions:
    """One encoded mint, spelled every way a CONSTANT expression can spell it.

    The fold reads the parse, so the case list is the grammar's: a Constant (which
    the parser has already merged implicit concatenation and every prefix into) and
    ``+`` over two of them. These rows are therefore not a list of spellings to keep
    extending -- they are witnesses that each spelling reaches the same two nodes.
    """

    @pytest.mark.parametrize(
        "expression",
        [
            f'"{_ENCODED_MINT}"',
            f'b"{_ENCODED_MINT}"',
            f'u"{_ENCODED_MINT}"',
            f'r"{_ENCODED_MINT}"',
            # implicit concatenation, two pieces and many, str and bytes
            " ".join(f'"{p}"' for p in _split(_ENCODED_MINT, 11)),
            " ".join(f'b"{p}"' for p in _split(_ENCODED_MINT, 11)),
            " ".join(f'"{p}"' for p in _split(_ENCODED_MINT, 4)),
            " ".join(f'b"{p}"' for p in _split(_ENCODED_MINT, 4)),
            # explicit ``+``, same shapes
            " + ".join(f'"{p}"' for p in _split(_ENCODED_MINT, 11)),
            " + ".join(f'b"{p}"' for p in _split(_ENCODED_MINT, 11)),
            " + ".join(f'"{p}"' for p in _split(_ENCODED_MINT, 4)),
            " + ".join(f'b"{p}"' for p in _split(_ENCODED_MINT, 4)),
            # mixed joiners, and a group -- parentheses are not nodes, so it folds free
            f'b"{_ENCODED_MINT[:8]}" + b"{_ENCODED_MINT[8:12]}" b"{_ENCODED_MINT[12:]}"',
            f'("{_ENCODED_MINT[:11]}" "{_ENCODED_MINT[11:]}")',
            f'("{_ENCODED_MINT[:11]}" + "{_ENCODED_MINT[11:]}")',
        ],
    )
    def test_every_constant_spelling_of_the_encoded_mint_is_denied(self, expression):
        cmd = f"python -c 'import base64; exec(base64.b64decode({expression}))'"
        assert _rule_of(cmd) == _MINT, expression

    @pytest.mark.parametrize(
        "expression",
        [
            f'"".join(["{_ENCODED_MINT[:11]}", "{_ENCODED_MINT[11:]}"])',
            f'"{_ENCODED_MINT[::-1]}"[::-1]',
            'bytes.fromhex("61326c79")',
            f'"%s" % "{_ENCODED_MINT}"',
            "encoded",
        ],
    )
    def test_a_computed_input_is_the_documented_residual(self, expression):
        """A value the parse cannot resolve is out of a STATIC floor's reach.

        This is the same residual a callable named at run time gets
        (``getattr(m, input())``): the floor states what it resolves rather than
        claiming a coverage it has no way to have. Pinned so a later change that
        starts resolving one of these is a deliberate widening, not a surprise.
        """
        cmd = f"python -c 'import base64; exec(base64.b64decode({expression}))'"
        assert _rule_of(cmd) is None, expression

    def test_the_fold_resolves_a_constant_node_and_refuses_a_computed_one(self):
        def value(source):
            return inline_payload._constant_text(ast.parse(source, mode="eval").body)

        assert value('"a" "b"') == "ab"
        assert value('b"a" b"b"') == "ab"
        assert value('"a" + "b" + "c"') == "abc"
        assert value('("a" "b") + "c"') == "abc"
        assert value('"".join(["a", "b"])') is None
        assert value('f"{x}"') is None
        assert value("name") is None
        assert value('"ab"[::-1]') is None
        assert value("1 + 2") is None

    def test_an_unparseable_payload_keeps_the_lexical_fold(self):
        """A shell-stripped or truncated payload has no parse to read.

        The lexical fold stays reachable for exactly that case, so a carrier the
        shell truncated does not silently stop folding. The literals here are whole
        and only the call is unclosed: a piece whose own quote is missing is not a
        string token to either fold, which is the tokenizer's limit, not a choice.
        """
        truncated = "exec(b64decode('a2lyb2Ny' 'ZXcgdG9rZW4='"
        assert ast_parse_fails(truncated)
        assert inline_payload._fold_inline_literals(truncated).count(_ENCODED_MINT[:-1]) == 1

    def test_a_folded_value_cannot_manufacture_a_statement_boundary(self):
        r"""A literal whose value carries a newline stays as written.

        The payload's source holds the two characters ``\n``; its VALUE holds a real
        newline. Emitting that folded would put the newline in the view, where the
        product-import anchor reads a newline as a statement start -- so the fold
        would invent an import statement, next to a credential word, that the
        payload never wrote. Left as written, the escape is not whitespace and
        starts no statement.
        """
        payload = "print('a;\\nimport kiro_crew, os\\ntoken')"
        folded = inline_payload._fold_inline_literals(payload)
        assert "\n" not in folded
        assert _rule_of(f'python -c "{payload}"') is None


def ast_parse_fails(source: str) -> bool:
    try:
        ast.parse(source)
    except SyntaxError:
        return True
    return False
