"""The two argv-floor questions that produced the audit log's false denials.

Eight days of ``security_events.jsonl`` held 179 bash-gate denials; 156 came from
two argv-floor predicates and none of those was the action the predicate exists
to stop:

* ``_is_credential_mint`` denied every inline Python program (``python -c``,
  ``python - <<'PY'``) that MENTIONED ``kiro_crew`` -- usually as the path of the
  file a patch script was editing -- or used ``getattr``/``eval``/``importlib``
  anywhere.  143 denials; 1 command contained the word ``token``; 0 minted.
* ``_is_self_kill`` read every brace group (``--jq '{state}'``, ``awk '{print $1}'``)
  as a word that "could expand to ``pkill``", and every glob argument
  (``ls -d dir/*``) likewise, then found a product-named path in a LATER word.
  13 denials; 8 commands contained no ``kill`` at all.

These tests pin the narrowed questions: an inline payload is a mint when it NAMES
the mint surface; a brace group expands to what bash expands it to; a glob handed
to a filesystem inspector, or a token that closes its own argv, is not a program.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.security import argv_floor, inline_payload
from kiro_crew.security.shell_normalizer import _glob_could_expand_to, _glob_to_regex

REPO_ROOT = Path(__file__).resolve().parent.parent
_MINT = "credential-exfil-kirocrew-token"
_KILL = "self-protection-kill"


def _rule_of(cmd: str) -> str | None:
    reason = security.is_denied(cmd)
    if reason is None:
        return None
    m = re.search(r"rule=(\S+)", reason)
    return m.group(1) if m else reason.splitlines()[0]


class TestInlinePayloadNamesTheMintSurface:
    """``_inline_payload_reaches_cli`` asks WHAT the payload names, not whether it mentions us."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # A patch script editing a product file: the package appears only as a path.
            "python3 - <<'PY'\nfrom pathlib import Path\n"
            "p = Path('src/kiro_crew/platform/governance.py')\n"
            "s = p.read_text()\np.write_text(s.replace('a', 'b'))\nPY",
            # A syntax check of a product file.
            "python3 -c \"import ast; ast.parse(open('src/kiro_crew/security.py').read())\"",
            # A dev one-liner importing an unrelated product module.
            "python3 -c 'from kiro_crew.acp._dispatch import parse_session_update as f; "
            "import inspect; print(inspect.signature(f))'",
            ".venv/bin/python -c 'from kiro_crew.platform.governance import BreakGlass; "
            "print(BreakGlass)'",
            # Loading a test module by path -- importlib is not a mint.
            "cd /w/kirocrew-wt-x && python3 -c 'import importlib.util, pathlib; "
            'p = pathlib.Path("test/test_parity.py"); '
            's = importlib.util.spec_from_file_location("t", p); print(s)\'',
            # getattr / eval / exec / b64decode one-liners next to a product path.
            "cd /w/kirocrew-wt-x && python3 -c \"import json; print(getattr(json, 'dumps'))\"",
            "python3 -c 'print(eval(\"1+1\"))' src/kiro_crew/x.py",
            "python3 - <<'EOF'\nimport base64\nprint(base64.b64decode('aGVsbG8='))\nEOF",
            # A mention of the credential words outside an import is a mention.
            "python3 -c \"print('kiro_crew docs mention the token verb')\"",
            "python3 -c \"print('the gateway keeps its secret in .local_secret')\"",
            "python3 -c 'import secrets; print(secrets.token_hex(8))'",
            # ``secrets`` (the stdlib module) next to a product import is not the word.
            "python3 -c 'import secrets; from kiro_crew.acp import client; print(secrets)'",
            # A product module named ``secrets`` is a path or an import, not the reader word.
            'python3 -c "from pathlib import Path; '
            "p = Path('src/kiro_crew/dashboard/handlers/secrets.py'); print(p.read_text()[:10])\"",
            "python3 -c 'from kiro_crew.dashboard.handlers import secrets; print(secrets)'",
            # The bare package import reaches nothing: kiro_crew/__init__ imports no CLI.
            "python3 -c 'import kiro_crew; print(kiro_crew.__file__)'",
            # A dynamic runner handed some OTHER name is generic code, even next to a product
            # import or a product path: ``__import__("os").environ`` is a common idiom.
            "python3 -c \"import runpy; runpy.run_path('src/kiro_crew/../../scripts/x.py')\"",
            "python3 -c \"import importlib; m = importlib.import_module('json'); "
            "print(m.dumps({'p': 'src/kiro_crew/x.py'}))\"",
            "python3 - <<'PY'\nfrom kiro_crew.acp.client import _opencode_agent_permissions\n"
            "env = {**__import__('os').environ, 'X': '1'}\nPY",
            # A code loader on some OTHER file is a patch-script idiom; the program name in a
            # subprocess argv is the interpreter-argv companion rule's business, not a load.
            "python3 -c \"exec(open('patch.py').read())\"",
            "python3 -c \"from pathlib import Path; exec(Path('/w/kirocrew-scratch/x.py').read_text())\"",
            "python3 -c \"import subprocess; print(subprocess.run(['kirocrew','status']))\"",
            # A directory NAMED for the product is not the console script: the loader loads
            # the file under it.
            "python3 -c \"import runpy; runpy.run_path('/home/dev/kirocrew/scripts/gen_docs.py')\"",
            "py -3 -c \"import runpy; runpy.run_path(r'C:\\Users\\dev\\kirocrew\\scripts\\gen_docs.py')\"",
            "python3 -c \"exec(open('/home/dev/kirocrew/scripts/x.py').read())\"",
        ],
    )
    def test_a_payload_that_only_mentions_the_package_is_allowed(self, cmd):
        assert not argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # The CLI dispatch, in every import spelling.
            "python -c 'import kiro_crew.cli as c; c.main()'",
            "python -c 'from kiro_crew import cli; cli.main()'",
            "python -c 'from kiro_crew.cli import main; main()'",
            "python -c 'import kiro_crew.__main__'",
            # The module implementing the token subcommand, and the token producers.
            "python -c 'from kiro_crew.cli_server import _token; _token(None)'",
            "python -c 'from kiro_crew.dashboard.token_auth import generate_token'",
            "python -c 'import kiro_crew.dashboard.token_secret as s; print(s._get_secret())'",
            "python -c 'from kiro_crew.dashboard import token_auth'",
            "python -c 'from kiro_crew.instances.token_mint import build_remote_token_command'",
            # The SECOND credential: the internal secret ``/api/token/local`` accepts, read
            # through product code past the sensitive-path floor that fences the file.
            "python -c 'from kiro_crew.config.loader import read_local_secret; "
            "print(read_local_secret(5476))'",
            "python -c 'from kiro_crew.instances import run_marker; print(run_marker.read_secret(5476))'",
            # ... and the module whose PATH names it.
            "python -c 'from kiro_crew.dashboard import token_secret'",
            # The whole route: import the reader, present the secret to the mint endpoint.
            # Both the reader's name and the endpoint carry the word.
            "python3 - <<'PY'\nimport urllib.request\n"
            "from kiro_crew.config.loader import read_local_secret\n"
            "s = read_local_secret(5476)\n"
            "r = urllib.request.Request('http://127.0.0.1:5476/api/token/local', "
            "headers={'X-Internal-Secret': s})\nprint(urllib.request.urlopen(r).read())\nPY",
            # The package anywhere in an ``import`` list is imported.
            "python -c 'import os, kiro_crew.instances.run_marker as r; print(r.read_secret(5476))'",
            "python -c 'import sys, os, kiro_crew.config.loader as l; print(l.read_local_secret(1))'",
            # A decoded literal is asked the same questions as the payload.
            # (``import kiro_crew.instances.run_marker as r; print(r.read_secret(5476))``)
            "python -c 'import base64; exec(base64.b64decode("
            '"aW1wb3J0IGtpcm9fY3Jldy5pbnN0YW5jZXMucnVuX21hcmtlciBhcyByOyBwcmludChyLnJlYWRfc2VjcmV0KDU0NzYpKQ=="))\'',
            # (``runpy.run_module('kiro_crew', run_name='__main__')``)
            "python -c 'import base64; exec(base64.b64decode("
            '"cnVucHkucnVuX21vZHVsZSgna2lyb19jcmV3JywgcnVuX25hbWU9J19fbWFpbl9fJyk="))\'',
            # The reader's name spelled in pieces is folded before matching.
            'python -c "from kiro_crew.config import loader; '
            "print(getattr(loader, 'read_local_' + 'secret')(5476))\"",
            # The price of the word: a product import next to a variable named ``secret``.
            "python3 -c 'from kiro_crew.acp._frame_record import scrub_frame; "
            'secret = "Basic abc="; print(scrub_frame({"Authorization": secret}))\'',
            # The stdin forms of the same.
            "python3 - <<'PY'\nfrom kiro_crew.dashboard.token_auth import generate_token\nPY",
            "python3 < src/kiro_crew/cli.py",
            "echo 'import kiro_crew.cli' | python3 -",
            # A module name split across concatenated pieces is folded before matching.
            "python -c \"__import__('kiro_crew.c' + 'li').cli.main()\"",
            "python -c \"m = 'kiro_crew.' + 'cli_server'; __import__(m)\"",
            # ``python -m kiro_crew token`` spelled as a dynamic runner call: the package
            # name is bare (no dotted surface) and ``__main__`` is a separate literal, so
            # only the runner-plus-package-name reach catches it.
            "python3 -c \"import runpy,sys; sys.argv=['x','token']; "
            "runpy.run_module('kiro_crew', run_name='__main__')\"",
            "python3 -c \"import runpy; runpy.run_module('kiro_crew', run_name='__main__')\"",
            "python3 -c \"import importlib; importlib.import_module('kiro_crew').cli.main()\"",
            "python3 -c \"__import__('kiro_crew')\"",
            # The argument spelled in pieces is folded before the runner is matched.
            "python3 -c \"import runpy; runpy.run_module('kiro_' + 'crew', run_name='__main__')\"",
            "python3 - <<'PY'\nimport runpy\nrunpy.run_module('kiro_crew', run_name='__main__')\nPY",
            # The mint hidden in a base64 literal is decoded and read.
            "python -c 'import base64; exec(base64.b64decode(\"aW1wb3J0IGtpcm9fY3Jldy5jbGk=\"))'",
            "python -c 'import os,base64; os.system(base64.b64decode(\"a2lyb2NyZXcgdG9rZW4=\").decode())'",
            # The INSTALLED CONSOLE SCRIPT run through a code loader: its text is
            # ``kiro_crew._bootstrap.main()``, the dispatch, against a ``sys.argv`` the payload
            # chose.  Resolved by ``which``, by path, by ``runpy.run_path``, folded, in a
            # heredoc, and one base64 decode away.
            "python3 -c \"import sys,shutil; sys.argv=['x','token']; exec(open(shutil.which('kirocrew')).read())\"",
            "python3 -c \"exec(open(shutil.which('kirocrew')).read())\"",
            "python3 -c \"exec(open('/opt/venv/bin/kirocrew').read())\"",
            "python3 -c \"import runpy,shutil; runpy.run_path(shutil.which('kiro-crew'), run_name='__main__')\"",
            "python3 -c \"eval(compile(open(shutil.which('kirocrew')).read(), 'k', 'exec'))\"",
            "python3 -c \"exec(open(shutil.which('kiro' + 'crew')).read())\"",
            "python3 - <<'PY'\nimport shutil\nexec(open(shutil.which('kirocrew')).read())\nPY",
            "python3 -c \"exec(__import__('base64').b64decode('ZXhlYyhvcGVuKHNodXRpbC53aGljaCgna2lyb2NyZXcnKSkucmVhZCgpKQ=='))\"",
            "python3 -c \"exec(open(r'C:\\venv\\Scripts\\kirocrew.exe').read())\"",
        ],
    )
    def test_a_payload_that_names_the_mint_surface_is_denied(self, cmd):
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) == _MINT, cmd

    def test_the_verb_gated_module_form_is_untouched(self):
        # `-m kiro_crew token` and `-c <payload naming the package> token` keep their own
        # verb-gated check; the inline gate is the addition for the verb-less forms.
        assert _rule_of("python -m kiro_crew token") == _MINT
        assert _rule_of("python -c 'from kiro_crew.acp import x' token") == _MINT
        assert _rule_of("python -m kiro_crew doctor") is None

    def test_base64_literals_are_decoded_from_the_command_as_submitted(self):
        """The floor reads a lower-cased view; base64 is case-sensitive and does not survive it."""
        raw = "python -c 'exec(b64decode(\"aW1wb3J0IGtpcm9fY3Jldy5jbGk=\"))'"
        assert inline_payload._decoded_b64_literals(raw) == ("import kiro_crew.cli",)
        # The lower-cased literal decodes to noise, never to the name -- which is why
        # ``is_denied`` hands the mint predicate the raw text, and why a caller with only
        # the lower-cased text sees the literal fold in nothing.
        assert not any("kiro_crew" in d for d in inline_payload._decoded_b64_literals(raw.lower()))
        assert not argv_floor._is_credential_mint(raw.lower())
        assert argv_floor._is_credential_mint(raw.lower(), raw_text=raw)

    def test_string_concatenation_is_folded_before_matching(self):
        assert "kiro_crew.cli" in inline_payload._fold_inline_literals(
            "__import__('kiro_crew.c' + 'li')"
        )
        assert "kiro_crew.cli_server" in inline_payload._fold_inline_literals(
            "x = 'kiro_' + 'crew.' + 'cli_server'"
        )
        # Python concatenates across quote kinds, so the fold does too.
        assert "kiro_crew.cli" in inline_payload._fold_inline_literals("'kiro_crew.c' + \"li\"")
        # A ``+`` between a literal and a NAME is not folded; nothing is invented.
        assert "kiro_crew.cli" not in inline_payload._fold_inline_literals("'kiro_crew.c' + li")
        # Implicit concatenation -- adjacent literals with nothing but whitespace between
        # them -- is the same join in Python, so it is the same fold.
        assert "kiro_crew.cli" in inline_payload._fold_inline_literals("'kiro_crew.c' 'li'")
        assert "kiro_crew.cli" in inline_payload._fold_inline_literals("'kiro_crew.c'  \"li\"")
        # A comma is two arguments, not one string.
        assert "kiro_crew.cli" not in inline_payload._fold_inline_literals("'kiro_crew.c', 'li'")
        assert (
            _rule_of("python -c \"import importlib; importlib.import_module('kiro_' 'crew.cli')\"")
            == _MINT
        )
        assert _rule_of("python -c \"m='kiro_crew.c' 'li'; __import__(m)\"") == _MINT
        assert _rule_of("python -c \"print('kiro_crew' ' docs mention token')\"") is None

    def test_the_decode_and_fold_are_bounded(self):
        many = " + ".join(["'a'"] * 500)
        folded = inline_payload._fold_inline_literals(many)
        assert folded.count("+") >= 500 - inline_payload._INLINE_LITERAL_FOLD_CAP - 1
        literals = " ".join(f"'{'QUJD' * 4}'" for _ in range(200))
        assert (
            len(inline_payload._decoded_b64_literals(literals))
            == inline_payload._INLINE_LITERAL_FOLD_CAP
        )

    def test_the_mint_surface_covers_every_token_producer_in_the_tree(self):
        """Every module that can produce a dashboard token is in reach of the gate -- derived
        from the tree, not from a list.

        The seed is the code that touches the signing key: a function that names
        ``token_signing.key`` AND opens a file (the fences that merely list the name are not
        it).  From the seed module the reach follows the import graph two hops -- the module
        that imports the key reader (``token_auth``, which signs) and every module that
        imports a signer from it (``generate_token`` re-exported under a path with no
        ``token`` in it).  Each (module, imported name) pair must be a reach in both spellings
        an inline program has -- ``from M import f`` and ``import M as m`` then ``m.f`` -- and
        so must every console-script entry point ``pyproject.toml`` declares.  Past hop two the
        RE-EXPORT chain is followed to a fixed point: a module that imports a producer name is
        itself an import source for that name, however deep.  Only the re-export relation is
        closed, not each module's call graph -- closing calls at every hop pulls the whole
        dashboard in through ``start_api_server`` and takes over a minute, while the producers
        are DEFINED at hop one and only re-exported after it.  A new producer fails here unless
        its module path or its NAME carries ``token``, which is the convention the gate rests on.
        """
        import ast
        import re
        import tomllib

        src = REPO_ROOT / "src"
        modules: dict[str, ast.Module] = {}
        for path in (src / "kiro_crew").rglob("*.py"):
            rel = path.relative_to(src).with_suffix("")
            if "_vendor" in rel.parts or "tests" in rel.parts:
                continue
            name = ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)
            try:
                modules[name] = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue

        def names_used(node: ast.AST) -> set[str]:
            out: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    out.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    out.add(sub.attr)
                elif isinstance(sub, ast.Constant) and sub.value == "token_signing.key":
                    out.add("token_signing.key")
            return out

        opens = {"open", "read_bytes", "write_bytes", "fdopen", "O_RDONLY", "O_CREAT"}
        seeds: set[str] = set()
        for mod, tree in modules.items():
            if "token_signing.key" not in names_used(tree):
                continue
            # The key's name, as the module spells it: the literal itself, or a module-level
            # constant bound to it (``_SECRET_KEY_FILE = "token_signing.key"``).  A module
            # that lists the file name inside a fence table binds no such constant.
            key_names = {"token_signing.key"} | {
                target.id
                for node in tree.body
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and node.value.value == "token_signing.key"
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                used = names_used(node)
                if used & key_names and used & opens:
                    seeds.add(mod)
                    break
        assert seeds == {"kiro_crew.dashboard.token_secret"}, seeds

        # Every ``from X import y [as z]`` in the tree, indexed by X: the re-export relation
        # the fixed point below walks, built once.
        import_from: dict[str, list[tuple[str, str, str]]] = {}
        for mod, tree in modules.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        import_from.setdefault(node.module, []).append(
                            (mod, alias.name, alias.asname or alias.name)
                        )

        def importers_of(targets: set[str], names: set[str] | None = None) -> list[tuple[str, str]]:
            return [
                (mod, bound)
                for target in sorted(targets)
                for mod, name, bound in import_from.get(target, ())
                if names is None or name in names
            ]

        def producers_in(mod: str, seed_names: set[str]) -> set[str]:
            """Functions of *mod* that reach a seed name, closed over the module's own calls."""
            tree = modules[mod]
            found = set(seed_names)
            changed = True
            while changed:
                changed = False
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.name not in found and names_used(node) & found:
                            found.add(node.name)
                            changed = True
            return found - seed_names

        # Hop 1: the modules that import the key reader, and which of THEIR functions
        # reach it (in ``token_auth``: ``_sign`` and every ``generate_*`` built on it --
        # not ``validate_token`` or ``is_csrf_exempt``, which never touch the key).
        hop1 = importers_of(seeds)
        hop1_producers: dict[str, set[str]] = {}
        for mod, name in hop1:
            hop1_producers.setdefault(mod, set()).add(name)
        for mod, seed_names in list(hop1_producers.items()):
            hop1_producers[mod] = producers_in(mod, seed_names) | seed_names
        assert "generate_token" in hop1_producers.get(
            "kiro_crew.dashboard.token_auth", set()
        ), hop1_producers
        # Hop 2: every module that imports one of those producers.
        hop2 = [
            pair for mod, names in hop1_producers.items() for pair in importers_of({mod}, names)
        ]
        # Hops 3..n: the re-export fixed point.  A module that imported a producer name is
        # an import source for that name; follow ``from <that module> import <name>`` until
        # no new (module, name) pair appears.
        reach_set = set(hop1) | set(hop2)
        frontier = set(hop2)
        while frontier:
            by_mod: dict[str, set[str]] = {}
            for mod, name in frontier:
                by_mod.setdefault(mod, set()).add(name)
            frontier = {
                pair
                for mod, names in by_mod.items()
                for pair in importers_of({mod}, names)
                if pair not in reach_set
            }
            reach_set |= frontier
        reaches = sorted(reach_set)
        assert any(name == "generate_token" for _, name in hop2), hop2
        assert len(reaches) >= 10, reaches

        # Key TOUCHERS that produce no token: they load or verify, and return nothing a
        # caller could present as a credential.  Each entry is a name the derivation
        # reaches through the key reader; a NEW name landing here has to be added with its
        # reason, or carry ``token`` in its name / module path so the gate covers it.
        verify_only = {
            "warm_auth_singletons": "loads the key into the process cache at startup; returns nothing",
            "extract_numeric_claim": "reads one claim out of a token it VERIFIES; returns the claim",
            "revoke_access_cookie": "adds a verified token's nonce to the denylist; returns a bool",
        }
        misses = []
        for mod in seeds:
            if not inline_payload._inline_payload_reaches_cli(f"import {mod}"):
                misses.append(f"import {mod}")
        for mod, name in reaches:
            if name in verify_only:
                continue
            for payload in (f"from {mod} import {name}", f"import {mod} as m\nm.{name}()"):
                if not inline_payload._inline_payload_reaches_cli(payload):
                    misses.append(payload)
        assert not misses, misses
        # ... and the exception list names only what the derivation actually reaches.
        reached_names = {name for _, name in reaches}
        assert set(verify_only) <= reached_names, set(verify_only) - reached_names

        scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["scripts"]
        for entry in scripts.values():
            entry_module = entry.split(":")[0]
            assert inline_payload._inline_payload_reaches_cli(
                f"import {entry_module}"
            ), entry_module
            assert re.match(r"kiro_crew\.", entry_module), entry_module

    def test_the_credential_word_covers_every_local_secret_reader_in_the_tree(self):
        """Every function that READS the internal secret is in reach of the gate -- derived
        from the tree, not from a list.

        The internal secret (``.local_secret``, or its per-listener ``run/gateway-<port>.secret``)
        is what ``/api/token/local`` accepts, so a product function that reads it is a
        credential reader the way a signer is a producer.  The seed is every function that
        NAMES the file -- the literal, or a module-level constant bound to it (``_SECRET_SUFFIX
        = ".secret"``) -- because the read itself may sit in a generic helper
        (``run_marker.read_secret`` delegates to ``_read_sidecar``).  The functions that name
        it without handing the credential to a caller are listed with their reason, as
        ``verify_only`` is above.  Every other one must be a reach in both spellings an inline
        program has, which holds only while its NAME or module path carries ``secret`` or
        ``token`` -- the convention ``_MINT_VERB_RE`` rests on.
        """
        import ast

        src = REPO_ROOT / "src"
        secret_files = {".local_secret", ".secret"}
        readers: list[tuple[str, str]] = []
        for path in (src / "kiro_crew").rglob("*.py"):
            rel = path.relative_to(src).with_suffix("")
            if "_vendor" in rel.parts or "tests" in rel.parts:
                continue
            mod = ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            file_names = set(secret_files) | {
                target.id
                for node in tree.body
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and node.value.value in secret_files
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                used: set[str] = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name):
                        used.add(sub.id)
                    elif isinstance(sub, ast.Attribute):
                        used.add(sub.attr)
                    elif isinstance(sub, ast.Constant) and sub.value in secret_files:
                        used.add(sub.value)
                if used & file_names:
                    readers.append((mod, node.name))
        # The derivation reaches the two readers the gate was narrowed past.
        assert ("kiro_crew.config.loader", "read_local_secret") in readers, readers
        assert ("kiro_crew.instances.run_marker", "read_secret") in readers, readers
        # Namers that hand no credential to a caller.  A NEW name landing here has to be
        # added with its reason, or carry ``secret`` / ``token`` so the gate covers it.
        not_readers = {
            "start_dashboard": "WRITES a fresh secret at gateway start; returns the app",
            "start_api_server": "WRITES a fresh secret at gateway start; returns the app",
            "_cron_dispatch": "presents the secret to the gateway for a CLI verb; returns an exit code",
            "_call_tool_inner": "presents the secret to the gateway for an MCP tool; returns tool text",
        }
        reached_names = {name for _, name in readers}
        assert set(not_readers) <= reached_names, set(not_readers) - reached_names
        misses = []
        for mod, name in readers:
            if name in not_readers:
                continue
            for payload in (f"from {mod} import {name}", f"import {mod} as m\nm.{name}()"):
                if not inline_payload._inline_payload_reaches_cli(payload):
                    misses.append(payload)
        assert not misses, misses

    def test_a_mention_is_not_a_reach(self):
        for mention in (
            "import kiro_crew",
            "from kiro_crew.acp import client",
            "from kiro_crew.security import is_denied",
            "path = 'src/kiro_crew/cli_help.py'",
            "print('kiro_crew tokens')",
            "import kiro_crew.clients",
            # `cli` as a fragment of another word after a product import is not the module.
            "from kiro_crew import security as s; print('restart is blocked by kiro-cli')",
            "from kiro_crew.acp import client; print(client.cli_help)",
            # the verb in a string with no product import
            "print('kirocrew token docs')",
            # a product import next to `tokens` (LLM tokens), not `token`
            "from kiro_crew.acp import client; print(client.max_tokens)",
            # a product import next to the stdlib `secrets` module, not `secret`
            "import secrets; from kiro_crew.acp import client; print(secrets.choice('ab'))",
            # the package later in an import list, with no credential word
            "import os, kiro_crew.acp.client as c; print(c)",
        ):
            assert not inline_payload._inline_payload_reaches_cli(mention), mention

    def test_a_split_base64_literal_is_folded_then_decoded(self):
        # `a2lyb2NyZXcgdG9rZW4=` is `kirocrew token`; split across two pieces it must still read.
        raw = 'python -c \'import os,base64; os.system(base64.b64decode("a2lyb2Ny" + "ZXcgdG9rZW4=").decode())\''
        assert inline_payload._decoded_b64_literals(raw) == ("kirocrew token",)
        assert _rule_of(raw) == _MINT
        # ... and split by implicit concatenation, which Python joins the same way.
        adjacent = 'python -c \'import os,base64; os.system(base64.b64decode("a2lyb2Ny" "ZXcgdG9rZW4=").decode())\''
        assert inline_payload._decoded_b64_literals(adjacent) == ("kirocrew token",)
        assert _rule_of(adjacent) == _MINT


class TestBraceExpansionMirrorsBash:
    """``_glob_to_regex`` translates ``{...}`` to what bash's brace expansion produces."""

    @pytest.mark.parametrize(
        "pattern,name,expected",
        [
            ("p{k,k}ill", "pkill", True),
            ("kiro{c..c}rew", "kirocrew", True),
            ("kiro{a..z}rew", "kirocrew", True),
            ("kiro{x,y}few", "kirocrew", False),
            # No top-level comma and not a sequence: bash leaves the braces literal.
            ("{state}", "pkill", False),
            ("{print $1}", "pkill", False),
            ("{directory}", "kirocrew", False),
            ("{pkill}", "pkill", False),
            # Alternatives are exact words, not "anything".
            ("{m:.mergeable,s:.state}", "pkill", False),
            ("{a,pkill}", "pkill", True),
            ("{a,{b,pkill}}", "pkill", True),
            ("{a,{b,c}}", "pkill", False),
            # An integer sequence is digits; no protected name carries digits.
            ("{1..9}", "pkill", False),
            ("pk{1..2}ll", "pkill", False),
            # A command substitution is literal text to this reader: what it prints is
            # outside a static floor, and its body is the payload walk's business (see
            # ``test_a_substitution_reads_literally_and_its_body_is_walked``).
            ("{$(printf pkill),-f}", "pkill", False),
            ("{`printf pkill`,-f}", "pkill", False),
            ("{a,b}$(x)", "pkill", False),
            ("{p,x}$(x)", "pkill", False),
            ("logs_$(hostname)_*", "pkill", False),
            ("{$(printf", "pkill", False),
            ("{a,$(printf", "pkill", False),
            # Arithmetic runs no command; parameter expansion stays literal, as it is at
            # program position (``$X -f <name>``), so awk field lists and prose naming
            # ``${name}`` are not verbs.
            ("{$((e-1)),d}", "pkill", False),
            ("{print $6,$7,$8,$9}", "pkill", False),
            ("{${name},%name%}", "pkill", False),
            ("{$X,-f}", "pkill", False),
            ("{print $1}", "pkill", False),
        ],
    )
    def test_expandability(self, pattern, name, expected):
        assert _glob_could_expand_to(pattern, frozenset({name})) is expected, (
            pattern,
            _glob_to_regex(pattern),
        )

    def test_deep_nesting_is_capped_not_recursed(self):
        """A word built hundreds of brace groups deep must answer, not crash the gate.

        The translation recurses once per nesting level, so with no cap a pathological
        word exhausts the interpreter stack inside ``is_denied`` and the tool decision
        aborts.  Past ``_BRACE_NESTING_CAP`` a group reads as ``.*`` -- the fail-closed
        direction -- and the command is still judged.
        """
        from kiro_crew.security.shell_normalizer import _BRACE_NESTING_CAP

        # Literal groups all the way down: bash leaves every one of them literal, so
        # the word starts with ``{`` and cannot be the program -- and the gate says so
        # instead of raising.
        literal_deep = "{" * 600 + "pkill" + "}" * 600
        assert _glob_could_expand_to(literal_deep, frozenset({"pkill"})) is False
        assert _rule_of(f"{literal_deep} -f kirocrew") is None
        # Alternatives all the way down: past the cap the inner group is ``.*``, so a
        # name it could produce is matched conservatively rather than analysed.
        alt_deep = "{a," * 600 + "pkill" + "}" * 600
        assert _glob_could_expand_to(alt_deep, frozenset({"pkill"})) is True
        assert _rule_of(f"{alt_deep} -f kirocrew") == _KILL
        # Under the cap the exact semantics hold; at the cap the same shape is read
        # conservatively.
        # (Nested alternations also spend the alternation budget, so "under" is under
        # both caps.)
        from kiro_crew.security.shell_normalizer import _BRACE_GROUP_BUDGET

        levels = min(_BRACE_NESTING_CAP, _BRACE_GROUP_BUDGET) - 1
        under = "{a," * levels + "b" + "}" * levels
        assert _glob_could_expand_to(under, frozenset({"pkill"})) is False
        at_cap = "{a," * (_BRACE_NESTING_CAP + 1) + "b" + "}" * (_BRACE_NESTING_CAP + 1)
        assert _glob_could_expand_to(at_cap, frozenset({"pkill"})) is True

    def test_unbalanced_braces_are_literal_and_linear(self):
        """A program word of N unmatched ``{`` must answer in O(N), and read literally.

        A pair lookup that scans from each ``{`` to the end of the word costs O(N^2) on N
        unmatched braces: a 12,000-brace word stalls the synchronous gate for tens of
        seconds, long enough for the loop watchdog to hard-exit the gateway.  ``_brace_pairs``
        resolves every pair in one pass; an unmatched brace is absent from it and is a literal.
        """
        import time

        from kiro_crew.security.shell_normalizer import _brace_pairs

        assert _brace_pairs("{a,{b}}c{") == {0: 6, 3: 5}
        assert _glob_to_regex("{{{kirocrew") == re.escape("{{{kirocrew")

        def judge(braces: int) -> float:
            hostile = "{" * braces + "kirocrew"
            started = time.perf_counter()
            assert _glob_could_expand_to(hostile, frozenset({"kirocrew"})) is False
            assert _rule_of(f"{hostile} token") is None
            assert _rule_of(f"{hostile} -f kirocrew") is None
            return time.perf_counter() - started

        # Linearity is a RATIO, not a wall-clock budget: a coverage-instrumented shared
        # CI runner is 10-20x slower than a desktop on the Python-level scan (the same
        # 12k-brace word measured 0.3s locally and 4.7s in CI), so an absolute bound
        # that catches the quadratic scan locally is either flaky there or too loose
        # to catch anything.  Both sizes run in the same process under the same load,
        # so their ratio is stable; 4x the braces cost 16x under the quadratic scan
        # and ~4x under the one-pass lookup.
        judge(300)  # warm the path so first-call cost is not billed to the small size
        small = judge(3_000)
        large = judge(12_000)
        assert large < 8 * small, f"3k braces {small:.3f}s, 12k braces {large:.3f}s"
        # Backstop for a stall the ratio cannot see (both sizes pathological).
        assert large < 30.0, f"12k braces took {large:.1f}s -- the gate would stall"

    def test_a_run_of_alternation_groups_is_budgeted_not_backtracked(self):
        """``{*,*}{*,*}...`` must answer in milliseconds, and fail closed past the budget.

        Each alternation is a regex branch pair whose branches are globs, so N of them
        give ``re`` 2^N ways to fail a short name: fifteen ``{*,*}`` groups take seconds
        per protected name, which is the gate stall the watchdog turns into a gateway
        exit.  Past ``_BRACE_GROUP_BUDGET`` a group reads as ``.*``, so the word is judged
        as "could be anything" -- over-matching a protected name, never missing one.
        """
        import time

        from kiro_crew.security.shell_normalizer import _BRACE_GROUP_BUDGET

        started = time.monotonic()
        for shape in ("{*,*}", "{*,**}", "{*,a*}"):
            # Past the budget the run reads as ``.*``: it could be ``pkill``, so denied.
            word = shape * 17 + "ill"
            assert _glob_could_expand_to(word, frozenset({"pkill"})) is True
            assert _rule_of(f"{word} -f kirocrew") == _KILL
            # A run that cannot end like a protected name is still answered, fast.
            assert _glob_could_expand_to(shape * 17 + "zz", frozenset({"pkill"})) is False
        # Nested alternations spend the same budget; past it the tail is ``.*``.
        nested = "{a,{b,c}}" * 17
        assert _glob_could_expand_to(nested + "zz", frozenset({"pkill"})) is False
        assert time.monotonic() - started < 1.0
        # Under the budget the alternatives are still read for what they are.
        under = "{a,b}" * (_BRACE_GROUP_BUDGET - 1) + "zz"
        assert _glob_could_expand_to(under, frozenset({"pkill"})) is False
        assert (
            _glob_could_expand_to(
                "{a,b}" * (_BRACE_GROUP_BUDGET - 1) + "pkill", frozenset({"pkill"})
            )
            is False
        )
        assert _glob_could_expand_to("{p,q}kill", frozenset({"pkill"})) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh pr view 9362 --repo o/KiroCrew --json state --jq '{state,mergeStateStatus}'",
            "gh api repos/o/KiroCrew/x --jq '{m:.mergeable,s:.mergeStateStatus}'",
            "awk '{print $1, $2}' f.txt; gh run view 1 --repo o/KiroCrew",
            "R=$KIROCREW_SCRATCH/r; join <(awk '{print $1}' \"$R/a\") <(awk '{print $1}' \"$R/b\")",
            "printf 'x: {directory}' > note; sed -i '1r /w/kirocrew-scratch/epd.py' f.py",
        ],
    )
    def test_a_quoted_brace_program_next_to_a_product_path_is_allowed(self, cmd):
        assert not argv_floor._is_self_kill(cmd.lower()), cmd
        assert _rule_of(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "p{k,k}ill -f kirocrew",
            "kiro{c..c}rew token",
            "{pkill,true} -f kirocrew",
        ],
    )
    def test_a_brace_group_that_expands_to_the_program_is_still_denied(self, cmd):
        assert _rule_of(cmd) in (_KILL, _MINT), cmd


class TestAGlobArgumentIsNotAKillProgram:
    @pytest.mark.parametrize(
        "cmd",
        [
            # Filesystem inspectors describe their arguments; a glob among them is a filename.
            "ls -d /w/KiroCrew-ws/* /h/w/KiroCrew-ws/* 2>/dev/null",
            "stat /w/kirocrew-wt-x/* | head",
            "du -sh /w/kirocrew/*",
            # A glob among a file mover's arguments is a path, whatever follows the
            # operator glued to it.
            "cp $KIROCREW_SCRATCH/retro/*; gh api repos/o/KiroCrew/issues/1/comments",
            "S=$KIROCREW_SCRATCH; mkdir -p $S/r; cp /tmp/a.txt $S/r/*; git diff origin/main -- src/kiro_crew",
        ],
    )
    def test_glob_argument_next_to_a_product_path_is_allowed(self, cmd):
        assert not argv_floor._is_self_kill(cmd.lower()), cmd
        assert _rule_of(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # A host-stamped filename -- literal text, then a substitution, then a glob --
            # under a program that is NOT a data consumer, with a product-named path later
            # in the same argv.  The word expands to something starting with its literal
            # prefix, so it cannot be the verb; the body (``hostname``) is harmless.  Every
            # row was confirmed newly-refused by the scope review of the revision that read
            # the whole word as ``.*``.
            "tar czf logs_$(hostname)_*.tar.gz -C /w/kirocrew-scratch/logs .",
            "zip -q /tmp/bench.zip bench_`hostname`_*.json /w/kirocrew-scratch/notes.md",
            "gh run download 12345678 --pattern coverage_$(hostname)_* --repo kirodotdev/KiroCrew",
            "install -m 644 report_$(hostname)_*.log /w/kirocrew-scratch/archive/",
            # The Windows spellings: a PowerShell ``$()`` subexpression is the same opener.
            "Copy-Item report_$(hostname)_*.log \\\\nas\\kirocrew\\archive\\",
            "Move-Item logs_$(hostname)_*.log C:\\kirocrew-scratch\\archive\\",
            "Compress-Archive -Path logs_$(hostname)_*.log -DestinationPath C:\\kirocrew-scratch\\logs.zip",
            "Get-ChildItem logs_$(hostname)_*.log C:\\kirocrew-scratch\\logs",
        ],
    )
    def test_a_prefixed_substitution_glob_argument_is_a_filename(self, cmd):
        assert not argv_floor._is_self_kill(cmd.lower()), cmd
        assert _rule_of(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # The same shape with a kill in the BODY is still a kill: the payload walk
            # judges the body wherever in the word it sits.
            "tar czf x_$(pkill -f kirocrew).tgz .",
            "zip -q out.zip a_`pkill -f kirocrew`_*",
        ],
    )
    def test_a_prefixed_substitution_whose_body_kills_is_denied(self, cmd):
        assert _rule_of(cmd) == _KILL, cmd

    @pytest.mark.parametrize(
        "cmd,rule",
        [
            # A symlink literally named with the operator: after quote removal the token
            # is ``/tmp/kirocrew;`` and reads like a closed argv.  The verb that follows is
            # still scanned, so the mint is denied.
            ("'/tmp/kirocrew;' token", _MINT),
            ('"kirocrew;" token', _MINT),
            ("'pkill;' -f kirocrew", _KILL),
            ('"/usr/bin/pkill&&" -f kirocrew', _KILL),
            # The over-denial this buys: a real boundary glued to the protected name is
            # read as if the next command's words were its arguments.  Safe direction.
            ("kirocrew; echo token", _MINT),
            ("pkill; echo kirocrew", _KILL),
            # The glued-operator PROGRAM spelling: the target follows it.
            ("x;pkill -f kirocrew", _KILL),
            ("x;kirocrew token", _MINT),
            ("sudo pkill -f kirocrew", _KILL),
        ],
    )
    def test_a_quoted_trailing_operator_does_not_close_the_argv(self, cmd, rule):
        assert _rule_of(cmd) == rule, cmd

    def test_a_substitution_reads_literally_and_its_body_is_walked(self):
        """The glob reader reads glob and brace syntax; a substitution is literal text.

        What a substitution PRINTS is outside a static reader -- at program position
        ``$(printf pk)ill -f <name>`` is the documented residual -- and its BODY is the
        payload walk's business, wherever in the word it sits.
        """
        pk = frozenset({"pkill"})
        assert _glob_to_regex("$(printf") == r"\$\(printf"
        assert _glob_could_expand_to("$(printf", pk) is False
        assert _glob_could_expand_to("logs_$(hostname)_*", pk) is False
        assert _glob_could_expand_to("{$(date", pk) is False
        assert _glob_could_expand_to("{a,$(printf", pk) is False
        # A matched group is read alternative by alternative; a substituted alternative
        # is that alternative's literal text.
        assert _glob_could_expand_to("{pkill,$(x)}", pk) is True
        assert _glob_could_expand_to("{$(x),y}", pk) is False
        assert _glob_could_expand_to("a{b,$(x)}c", pk) is False
        # Parameter expansion is literal too.
        assert _glob_to_regex("${X}_*") == r"\$\{X\}_.*"
        # The body is judged on its own, wherever it sits.
        assert _rule_of("{$(pkill -f kirocrew),x} y") == _KILL
        assert _rule_of("echo foo_$(pkill -f kirocrew)") == _KILL

    @pytest.mark.parametrize(
        "cmd",
        [
            # The body of a substitution RUNS before the consumer sees a word: no
            # data-consumer program excuses it.
            "ls $(kirocrew update)",
            "cp $(kirocrew restart) /tmp",
            "rm -f $(kirocrew gateway restart)",
            "echo $(kirocrew token)",
            "ls `kirocrew token`",
            'cat "$(kirocrew token)"',
            # ... wherever in the word the body sits.
            "ls {$(pkill -f kirocrew),y}",
            "mv {$(pkill -f kirocrew),x}.log /tmp/",
            "cp {a,$(pkill -f kirocrew)} /tmp",
            "ls {$(kirocrew update),y}",
            "ls x$(kirocrew update)",
            "cat /tmp/`kirocrew token`",
        ],
    )
    def test_a_substitution_under_a_data_consumer_is_not_exempt(self, cmd):
        assert _rule_of(cmd) is not None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "ls $(pwd)/src/kiro_crew",
            "echo $(date) kirocrew docs",
            "cp $(git rev-parse --show-toplevel)/src/kiro_crew/x.py /tmp/",
            # A substitution INSIDE a filename glob: the word reads as ``.*`` (so it
            # "could be" the verb), but it is a filename under a mover, and the body
            # (``hostname``) is harmless.  Confirmed newly-refused by the scope review
            # of an earlier revision, which refused every substitution-carrying word.
            "cp report_$(hostname)_*.log /w/kirocrew-scratch/archive/",
            "mv $(date +%F)_*.log /w/kirocrew-scratch/",
            "ls report_`hostname`_* /w/kirocrew/",
            # A substitution as the first alternative of a BRACE GROUP: the word is two
            # filenames, the body (``date``) is walked on its own.  Confirmed newly-refused
            # by the scope review of an earlier revision.
            "mv {$(date +%F),current}.log /w/kirocrew-scratch/archive/",
            "rsync -a {$(date +%F),default}.conf /w/kirocrew-scratch/etc/",
            "cp {$(hostname),local}.log /w/kirocrew-scratch/",
        ],
    )
    def test_a_harmless_substitution_under_a_data_consumer_is_allowed(self, cmd):
        assert _rule_of(cmd) is None, cmd
