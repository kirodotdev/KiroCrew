"""Signature scheme 2 binds an app's whole manifest and every packaged file.

Under ``require_signature`` the gate admits only a scheme-2 signature, and the files
under the app root must match the signed ``signedFiles`` inventory at install, update,
activation and every execution boundary given an app root. Files the inventory does
not list are tolerated. Without the policy nothing here changes a decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pytest

from conftest import make_dir_link, make_escaping_link
from kiro_crew.apps import admission, execution
from kiro_crew.apps.admission import (
    app_admission_denied,
    builtin_admission_exempt,
    installed_package_refusal,
    verified_signer,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.package_signature import (
    FILES_KEY,
    inventory_refusal,
    package_signature,
    sign_app_directory,
)

SIGNER = "acme"
SECRET = "publisher-trust-key"
APP = "signed-pkg"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home


def _enforce(home: Path) -> None:
    (home / "app_admission.json").write_text(
        json.dumps({"mode": "enforce", "require_signature": True, "trust_keys": {SIGNER: SECRET}}),
        encoding="utf-8",
    )


def _package(tmp_path: Path) -> Path:
    src = tmp_path / "source" / APP
    (src / "backend").mkdir(parents=True)
    (src / "ui").mkdir()
    (src / "node_modules" / "dep").mkdir(parents=True)
    (src / "app.json").write_text(
        json.dumps({"name": APP, "version": "1.0.0", "displayName": "Signed", "description": "d"}),
        encoding="utf-8",
    )
    (src / "backend" / "app.py").write_text("print('publisher')\n", encoding="utf-8")
    (src / "ui" / "index.mjs").write_text("export default 1;\n", encoding="utf-8")
    (src / "node_modules" / "dep" / "index.js").write_text("x\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    return src


def test_a_manifest_with_a_byte_order_mark_signs_and_is_admitted(tmp_path, home):
    src = _package(tmp_path)
    manifest = src / "app.json"
    manifest.write_bytes(b"\xef\xbb\xbf" + manifest.read_bytes())
    sign_app_directory(src, SIGNER, SECRET)
    assert _denied(src) is None


def _manifest(root: Path) -> AppManifest:
    return AppManifest.from_json_file(root / "app.json")


def _denied(root: Path) -> str | None:
    return app_admission_denied(APP, manifest=_manifest(root), action="install", app_root=root)


def test_the_inventory_lists_what_the_install_copy_carries(tmp_path, home):
    src = _package(tmp_path)
    (src / "data").mkdir()
    (src / "data" / "state.json").write_text("{}\n", encoding="utf-8")
    (src / ".app_secret").write_text("s\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    document = json.loads((src / "app.json").read_text(encoding="utf-8"))
    assert document["signatureScheme"] == 2
    assert sorted(document[FILES_KEY]) == ["backend/app.py", "ui/index.mjs"]
    assert document["signature"] == package_signature(SECRET, document)
    assert inventory_refusal(src, document) is None


def test_an_untouched_package_is_admitted_and_names_its_signer(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    assert _denied(src) is None
    assert verified_signer(_manifest(src)) == SIGNER


def test_a_changed_signed_file_is_refused(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    (src / "backend" / "app.py").write_text("print('attacker')\n", encoding="utf-8")
    assert "backend/app.py" in (_denied(src) or "")


def test_a_missing_signed_file_is_refused(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    (src / "ui" / "index.mjs").unlink()
    assert "ui/index.mjs" in (_denied(src) or "")


def test_a_signed_file_reached_through_a_link_is_refused(tmp_path):
    """A listed name that is a link (a junction on Windows) is not the signed file."""
    src = tmp_path / "pkg"
    (src / "backend").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("print('elsewhere')\n", encoding="utf-8")
    rel = make_escaping_link(src / "backend", outside)
    digest = hashlib.sha256((outside / "secret.py").read_bytes()).hexdigest()
    reason = inventory_refusal(src, {FILES_KEY: {f"backend/{rel}": digest}})
    assert reason is not None and f"backend/{rel}" in reason


def test_a_listed_directory_replaced_by_a_link_is_refused(tmp_path):
    """The same bytes behind a linked parent directory are refused too."""
    src = tmp_path / "pkg"
    src.mkdir()
    real = tmp_path / "real-backend"
    real.mkdir()
    (real / "app.py").write_text("print('publisher')\n", encoding="utf-8")
    make_dir_link(src / "backend", real)
    digest = hashlib.sha256((real / "app.py").read_bytes()).hexdigest()
    reason = inventory_refusal(src, {FILES_KEY: {"backend/app.py": digest}})
    assert "not inside a real directory" in (reason or "")


def test_a_listed_path_that_is_not_a_regular_file_is_refused(tmp_path):
    src = tmp_path / "pkg"
    (src / "backend" / "app.py").mkdir(parents=True)
    reason = inventory_refusal(src, {FILES_KEY: {"backend/app.py": "0" * 64}})
    assert "backend/app.py" in (reason or "")


def test_an_unlisted_file_is_tolerated(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    (src / "dist").mkdir()
    (src / "dist" / "bundle.js").write_text("built\n", encoding="utf-8")
    assert _denied(src) is None


def test_any_manifest_change_breaks_the_signature(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    document = json.loads((src / "app.json").read_text(encoding="utf-8"))
    document["description"] = "rewritten"
    (src / "app.json").write_text(json.dumps(document), encoding="utf-8")
    assert "re-sign" in (_denied(src) or "")


def test_a_rewritten_inventory_breaks_the_signature(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    (src / "backend" / "app.py").write_text("print('attacker')\n", encoding="utf-8")
    document = json.loads((src / "app.json").read_text(encoding="utf-8"))
    document[FILES_KEY].pop("backend/app.py")
    (src / "app.json").write_text(json.dumps(document), encoding="utf-8")
    assert "re-sign" in (_denied(src) or "")


@pytest.mark.parametrize("scheme", [1, True, "2", None])
def test_only_scheme_2_is_accepted(tmp_path, home, scheme):
    _enforce(home)
    src = _package(tmp_path)
    document = json.loads((src / "app.json").read_text(encoding="utf-8"))
    document["signatureScheme"] = scheme
    document["signature"] = package_signature(SECRET, document)
    (src / "app.json").write_text(json.dumps(document), encoding="utf-8")
    assert "re-sign" in (_denied(src) or "")


@pytest.mark.parametrize("rel", ["../escape.py", "/etc/passwd", "a//b", "a\\b", "c:x"])
def test_an_unusable_inventory_path_is_refused(tmp_path, rel):
    assert "unusable path" in (inventory_refusal(tmp_path, {FILES_KEY: {rel: "0" * 64}}) or "")


def test_without_the_policy_nothing_is_checked(tmp_path, home):
    src = _package(tmp_path)
    (src / "backend" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    assert _denied(src) is None
    assert installed_package_refusal(APP, src, _manifest(src)) is None
    assert builtin_admission_exempt("not-shipped", "builtin") is True


def test_a_forged_builtin_record_needs_a_shipped_package_under_the_policy(home):
    _enforce(home)
    assert builtin_admission_exempt("not-shipped", "builtin") is False
    assert builtin_admission_exempt("not-shipped", "registry") is False
    shipped = next(iter(execution._shipped_builtin_apps()))[1]
    assert builtin_admission_exempt(shipped, "builtin") is True


def test_execution_refuses_an_installed_file_changed_after_install(tmp_path, home):
    from kiro_crew.apps.manager import app_dir, install_app

    _enforce(home)
    assert install_app(_package(tmp_path)).ok
    root = app_dir(APP)
    run = root / "backend" / "app.py"
    gate = dict(action="backend_spawn", app_root=run, manifest=_manifest(root))
    assert execution.app_execution_denied(APP, **gate) is None
    run.write_text("print('attacker')\n", encoding="utf-8")
    assert "backend/app.py" in (execution.app_execution_denied(APP, **gate) or "")
    # A caller that passes no manifest of its own is covered by the gate before it.
    assert execution.app_execution_denied(APP, action="module_load", app_root=root) is None


def test_the_sign_command_reads_its_key_from_the_environment(tmp_path, monkeypatch):
    from kiro_crew.cli_commands_app import _handle_app_sign

    src = tmp_path / "pkg"
    src.mkdir()
    (src / "app.json").write_text(json.dumps({"name": "pkg", "version": "1"}), encoding="utf-8")
    (src / "main.py").write_text("x = 1\n", encoding="utf-8")
    args = argparse.Namespace(source=str(src), signer=SIGNER)
    monkeypatch.delenv("KIROCREW_APP_SIGNING_KEY", raising=False)
    with pytest.raises(SystemExit):
        _handle_app_sign(args)
    assert "signature" not in json.loads((src / "app.json").read_text(encoding="utf-8"))

    monkeypatch.setenv("KIROCREW_APP_SIGNING_KEY", SECRET)
    _handle_app_sign(args)
    document = json.loads((src / "app.json").read_text(encoding="utf-8"))
    assert list(document[FILES_KEY]) == ["main.py"]
    assert document["signature"] == package_signature(SECRET, document)


def test_the_admission_module_reads_the_policy_it_is_given(home):
    # Guards the fixture: the gate reads the home this file writes.
    _enforce(home)
    assert admission.load_app_admission_policy().require_signature is True


def test_a_digest_with_fresh_stamps_is_not_cached(tmp_path, monkeypatch):
    """A rewrite in the same timestamp tick, size kept and mtime put back, is hashed again.

    The clock is pinned to the file's own stamps, so the racy-stamp rule is what keeps
    the first digest out of the cache, not the clock moving on.
    """
    from kiro_crew.apps import package_signature

    monkeypatch.setattr(package_signature, "_DIGESTS", {})
    src = tmp_path / "pkg"
    src.mkdir()
    target = src / "main.py"
    target.write_text("x = 1\n", encoding="utf-8")
    old = 1_600_000_000_000_000_000
    os.utime(target, ns=(old, old))
    stamps = target.stat()
    monkeypatch.setattr(package_signature, "_now_ns", lambda: stamps.st_ctime_ns)
    document = {FILES_KEY: {"main.py": hashlib.sha256(target.read_bytes()).hexdigest()}}
    assert inventory_refusal(src, document) is None
    assert package_signature._DIGESTS == {}
    target.write_text("x = 2\n", encoding="utf-8")
    os.utime(target, ns=(old, old))
    assert "main.py" in (inventory_refusal(src, document) or "")


def test_a_digest_with_settled_stamps_is_cached_on_posix(tmp_path, monkeypatch):
    from kiro_crew.apps import package_signature

    monkeypatch.setattr(package_signature, "_DIGESTS", {})
    src = tmp_path / "pkg"
    src.mkdir()
    target = src / "main.py"
    target.write_text("x = 1\n", encoding="utf-8")
    stamps = target.stat()
    later = max(stamps.st_mtime_ns, stamps.st_ctime_ns) + 10 * package_signature._RACY_WINDOW_NS
    monkeypatch.setattr(package_signature, "_now_ns", lambda: later)
    document = {FILES_KEY: {"main.py": hashlib.sha256(target.read_bytes()).hexdigest()}}
    assert inventory_refusal(src, document) is None
    assert bool(package_signature._DIGESTS) is package_signature.IS_POSIX


def _declare(src: Path, **fields: object) -> None:
    """Add *fields* to the signed manifest and re-sign it as the publisher would."""
    document = json.loads((src / "app.json").read_text(encoding="utf-8"))
    document.update(fields)
    document["signature"] = package_signature(SECRET, document)
    (src / "app.json").write_text(json.dumps(document), encoding="utf-8")


def test_a_named_entry_point_missing_from_signed_files_is_refused(tmp_path, home):
    src = _package(tmp_path)
    document = json.loads((src / "app.json").read_text(encoding="utf-8"))
    del document[FILES_KEY]["backend/app.py"]
    _declare(src, backend={"entryPoint": "backend/app.py"}, signedFiles=document[FILES_KEY])
    assert _denied(src) is None  # no policy: unchanged
    _enforce(home)
    assert "backend/app.py" in (_denied(src) or "")


def test_a_command_outside_the_app_is_not_a_packaged_file(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    _declare(src, mcpServers={"tools": {"command": "python3", "args": ["-m", "json.tool"]}})
    assert _denied(src) is None


def test_a_file_an_mcp_argument_names_must_be_signed(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    (src / "server.py").write_text("print('unsigned')\n", encoding="utf-8")
    _declare(src, mcpServers={"tools": {"command": "python3", "args": ["server.py"]}})
    assert "server.py" in (_denied(src) or "")


def test_a_hook_needs_its_exact_module_file_signed_wherever_it_lives(tmp_path, home):
    """A hook loads ``{base}.py`` only, and a code path under ``data/`` is no exception."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "backend" / "hooks").mkdir()
    (src / "backend" / "hooks" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    # ``backend/hooks/__init__.py`` is listed; the file the loader opens is not.
    (src / "backend" / "hooks.py").write_text("print('unsigned')\n", encoding="utf-8")
    _declare(src, backend={"hooks": {"on_startup": "backend.hooks:start"}})
    assert "backend/hooks.py" in (_denied(src) or "")

    src2 = tmp_path / "second" / APP
    (src2 / "data").mkdir(parents=True)
    (src2 / "app.json").write_text(
        json.dumps({"name": APP, "version": "1.0.0", "displayName": "S", "description": "d"}),
        encoding="utf-8",
    )
    sign_app_directory(src2, SIGNER, SECRET)
    (src2 / "data" / "evil.py").write_text("print('data code')\n", encoding="utf-8")
    _declare(src2, backend={"hooks": {"on_startup": "data.evil:go"}})
    assert "data/evil.py" in (_denied(src2) or "")


def test_a_data_file_named_in_args_does_not_count(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    (src / "server.py").write_text("print('server')\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    (src / "state.db").write_text("rows\n", encoding="utf-8")
    _declare(
        src,
        mcpServers={"tools": {"command": "python3", "args": ["server.py", "--db", "state.db"]}},
    )
    assert _denied(src) is None


def test_a_path_that_normalises_out_of_the_app_is_refused(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    _declare(src, backend={"entryPoint": "backend/../../outside.py"})
    assert "leaves the app folder" in (_denied(src) or "")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python", True),
        ("python3", True),
        ("python3.12", True),
        ("pythonw", True),
        ("/opt/venv/bin/python3", True),
        ("C:\\Py\\python.exe", True),
        ("python-lsp-server", False),
        ("pythonmcp", False),
        ("node", False),
        ("bash", False),
    ],
)
def test_only_a_python_interpreter_is_one(command, expected):
    from kiro_crew.apps.package_signature import is_python_interpreter

    assert is_python_interpreter(command) is expected


def test_python_m_checks_the_package_python_would_run(tmp_path, home):
    """``-m worker`` runs ``worker/__init__.py`` and ``worker/__main__.py`` when that
    package exists, so a signed ``worker.py`` beside it covers nothing."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "worker.py").write_text("print('signed module')\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    (src / "worker").mkdir()
    (src / "worker" / "__init__.py").write_text("", encoding="utf-8")
    (src / "worker" / "__main__.py").write_text("print('unsigned package')\n", encoding="utf-8")
    _declare(src, mcpServers={"w": {"command": "python3", "args": ["-m", "worker"]}})
    assert "worker/__init__.py" in (_denied(src) or "")


def test_attached_python_m_resolves_like_the_spaced_form(tmp_path, home):
    """Python reads ``-mworker`` (and ``-Bmworker``) as ``-m worker``: same package check."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "worker.py").write_text("print('signed module')\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    (src / "worker").mkdir()
    (src / "worker" / "__init__.py").write_text("", encoding="utf-8")
    (src / "worker" / "__main__.py").write_text("print('unsigned package')\n", encoding="utf-8")
    for args in (["-mworker"], ["-Bmworker"]):
        _declare(src, mcpServers={"w": {"command": "python3", "args": args}})
        assert "worker/__init__.py" in (_denied(src) or ""), args


def test_a_case_variant_entry_point_counts_only_as_the_same_file(tmp_path, home):
    """Signed as ``Worker/Run.py`` and declared ``worker/run.py``: admitted where the
    volume folds case (the same file); refused where the twin is a different file."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "Worker").mkdir()
    (src / "Worker" / "Run.py").write_text("print('signed')\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    _declare(src, backend={"entryPoint": "worker/run.py"})
    if os.path.lexists(src / "worker" / "run.py"):  # case-insensitive volume
        assert _denied(src) is None
    else:
        (src / "worker").mkdir()
        (src / "worker" / "run.py").write_text("print('unsigned')\n", encoding="utf-8")
        assert "worker/run.py" in (_denied(src) or "")


def test_an_extensionless_mcp_command_inside_the_app_must_be_signed(tmp_path, home):
    """Rule: a command word that names a file in the app folder is code, any extension."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "launch").write_text("#!/bin/sh\necho unsigned\n", encoding="utf-8")
    _declare(src, mcpServers={"tools": {"command": "launch"}})
    assert "launch" in (_denied(src) or "")


def test_a_system_command_word_names_no_packaged_file(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    _declare(src, mcpServers={"tools": {"command": "npx", "args": ["-y", "some-server"]}})
    assert _denied(src) is None


def test_an_unsigned_package_beside_a_signed_module_is_refused(tmp_path, home):
    """Python imports ``helpers/`` before ``helpers.py``, so the package must be signed too."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "helpers.py").write_text("VALUE = 'signed'\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    (src / "helpers").mkdir()
    (src / "helpers" / "__init__.py").write_text("VALUE = 'unsigned'\n", encoding="utf-8")
    assert "helpers/__init__.py" in (_denied(src) or "")


def test_a_signed_package_beside_a_signed_module_is_admitted(tmp_path, home):
    _enforce(home)
    src = _package(tmp_path)
    (src / "helpers.py").write_text("VALUE = 'module'\n", encoding="utf-8")
    (src / "helpers").mkdir()
    (src / "helpers" / "__init__.py").write_text("VALUE = 'package'\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    assert _denied(src) is None


def test_an_unsigned_session_control_entry_is_refused(tmp_path, home):
    """A contributed ``sessionControls`` entry is served from ``ui/`` and must be signed."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "ui" / "chip.mjs").write_text("export default 'unsigned';\n", encoding="utf-8")
    _declare(
        src,
        contributes={"sessionControls": [{"id": "c", "entryPoint": "chip.mjs", "label": "C"}]},
    )
    assert "ui/chip.mjs" in (_denied(src) or "")


def test_a_home_placeholder_argument_is_checked_as_the_path_it_expands_to(
    tmp_path, home, monkeypatch
):
    """``${HOME}/…`` in an MCP argument is expanded as the runtime expands it, so an
    unsigned ``data/server.py`` it names is refused."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "data").mkdir()
    (src / "data" / "server.py").write_text("print('unsigned')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(src.parent))
    _declare(
        src,
        mcpServers={"tools": {"command": "python3", "args": [f"${{HOME}}/{APP}/data/server.py"]}},
    )
    assert "data/server.py" in (_denied(src) or "")


def test_an_extensionless_file_entry_point_must_be_signed(tmp_path, home):
    """``launch`` exists as a file, so the spawn runs that literal path."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "launch").write_text("#!/bin/sh\necho unsigned\n", encoding="utf-8")
    _declare(src, backend={"entryPoint": "launch"})
    assert "launch" in (_denied(src) or "")


def test_a_contributed_ui_entry_is_checked_under_ui(tmp_path, home):
    """A panel tab's ``entry`` is served from ``ui/``; a signed root-level twin covers nothing."""
    _enforce(home)
    src = _package(tmp_path)
    (src / "tab.mjs").write_text("export default 'signed twin';\n", encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    (src / "ui" / "tab.mjs").write_text("export default 'unsigned';\n", encoding="utf-8")
    _declare(
        src,
        contributes={"panelTabs": [{"id": "t", "title": "T", "entry": "tab.mjs"}]},
    )
    assert "ui/tab.mjs" in (_denied(src) or "")
