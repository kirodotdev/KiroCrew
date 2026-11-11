"""A signed app's signature binds its whole manifest and every packaged file.

Scheme 2 of the App Kit signature: ``AppManifest.signing_payload`` is the whole
manifest document (minus ``signature`` and the ``l10n`` listing copy), and the
manifest's ``fileDigests`` names each packaged file with its SHA-256
(``apps.signed_content``). Under ``require_signature`` admission checks the tree
wherever one is at hand, and each executable surface re-checks the installed tree
just before it runs (``admission.installed_content_refusal``).
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import pytest
from apps_backend_coverage_helpers import spawn_root_fixture  # noqa: F401

from kiro_crew.apps import admission
from kiro_crew.apps import manifest as manifest_module
from kiro_crew.apps.admission import AppAdmissionPolicy, _signature_valid, app_admission_denied
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.testing.links import make_dir_link

# The names under test are bound at import but resolved when a test runs, so a
# tree without them fails each test where it calls them, not at collection.
try:
    from kiro_crew.apps import signed_content
    from kiro_crew.apps.signed_content import (
        InventoryError,
        inventory_mismatch,
        packaged_inventory,
        sign_app_dir,
    )
except ImportError:
    signed_content = None  # type: ignore[assignment]
    InventoryError = inventory_mismatch = packaged_inventory = sign_app_dir = None  # type: ignore
installed_content_refusal = getattr(admission, "installed_content_refusal", None)
APP_SIGNATURE_SCHEME = getattr(manifest_module, "APP_SIGNATURE_SCHEME", None)

SECRET = "publisher-secret"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(root: Path, rel: str, data: bytes | str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


def _app(root: Path, name: str = "probe", **extra: object) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": name,
        "description": "probe",
        "permissions": {},
        "backend": {"entryPoint": "backend/server.py", "type": "python"},
        **extra,
    }
    (root / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write(root, "backend/server.py", "print('published')\n")
    _write(root, "ui/index.js", "export default 1;\n")
    return root


@pytest.fixture
def signing_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home whose fleet policy requires signatures from publisher ``acme``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(admission, "config_dir", lambda: home)
    (home / "app_admission.json").write_text(
        json.dumps({"mode": "enforce", "require_signature": True, "trust_keys": {"acme": SECRET}}),
        encoding="utf-8",
    )
    return home


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------


class TestPackagedInventory:
    def test_lists_every_packaged_file_with_its_digest(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        inv = packaged_inventory(root)
        assert inv == {
            "backend/server.py": _sha(b"print('published')\n"),
            "ui/index.js": _sha(b"export default 1;\n"),
        }
        assert list(inv) == sorted(inv)

    def test_leaves_out_what_an_install_never_carries(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        for rel in (
            "node_modules/x/index.js",
            "ui/node_modules/y.js",
            "backend/__pycache__/server.cpython-312.pyc",
            ".git/HEAD",
            ".venv/bin/python",
            "installed.json",
            "data/state.json",
            ".app_secret",
        ):
            _write(root, rel, "x")
        assert set(packaged_inventory(root)) == {"backend/server.py", "ui/index.js"}

    @pytest.mark.skipif(sys.platform == "win32", reason="a colon is not a Windows name")
    def test_a_name_no_signed_list_can_carry_is_refused(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        _write(root, "ui/a:b.js", "x")
        with pytest.raises(InventoryError, match="backslash or colon"):
            packaged_inventory(root)

    def test_a_nested_data_directory_is_package_content(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        _write(root, "ui/data/strings.json", "{}")
        assert "ui/data/strings.json" in packaged_inventory(root)

    @pytest.mark.parametrize("kind", ["file", "dir"])
    def test_a_link_is_refused(self, tmp_path: Path, kind: str) -> None:
        root = _app(tmp_path / "a")
        target = tmp_path / ("outside.py" if kind == "file" else "outside")
        link = root / "backend" / "helper"
        if kind == "file":
            target.write_text("evil", encoding="utf-8")
            try:
                link.symlink_to(target)
            except NotImplementedError:
                pytest.skip("file symlinks are unsupported")
            except OSError as exc:
                if (
                    exc.errno in {errno.ENOSYS, errno.ENOTSUP}
                    or getattr(exc, "winerror", None) == 1314
                ):
                    pytest.skip("file symlinks are unsupported or need unavailable privilege")
                raise
        else:
            target.mkdir()
            make_dir_link(link, target)
        with pytest.raises(InventoryError, match="is a link"):
            packaged_inventory(root)

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
    def test_a_special_file_is_refused(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        os.mkfifo(root / "backend" / "pipe")
        with pytest.raises(InventoryError, match="not a regular file"):
            packaged_inventory(root)

    def test_names_that_differ_only_by_case_are_refused(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        _write(root, "ui/Index.js", "other")
        if not (root / "ui" / "index.js").read_text().startswith("export"):
            pytest.skip("case-insensitive filesystem")
        with pytest.raises(InventoryError, match="differ only by case"):
            packaged_inventory(root)


class TestInventoryMismatch:
    def test_the_signed_tree_matches(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        assert inventory_mismatch(root, packaged_inventory(root)) is None

    def test_a_changed_file_is_named(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        declared = packaged_inventory(root)
        _write(root, "backend/server.py", "print('tampered')\n")
        assert inventory_mismatch(root, declared) == (
            "backend/server.py does not match its signed digest"
        )

    def test_an_added_file_is_named(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        declared = packaged_inventory(root)
        _write(root, "backend/json.py", "import os\n")
        assert inventory_mismatch(root, declared) == (
            "backend/json.py is not in the signed file list"
        )

    def test_a_removed_file_is_named(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        declared = packaged_inventory(root)
        (root / "ui" / "index.js").unlink()
        assert inventory_mismatch(root, declared) == "ui/index.js is signed but missing"

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "../outside.py",
            "backend/./server.py",
            "backend//server.py",
            "backend\\server.py",
            "C:/x.py",
            "app.json",
            "data/state.json",
            "node_modules/x.js",
            "backend/__pycache__/x.pyc",
            "",
        ],
    )
    def test_a_malformed_declared_path_is_refused(self, tmp_path: Path, path: str) -> None:
        root = _app(tmp_path / "a")
        declared = {**packaged_inventory(root), path: "0" * 64}
        # Refused for its shape, before any comparison with the tree.
        assert (inventory_mismatch(root, declared) or "").startswith("fileDigests")

    def test_declared_paths_that_differ_only_by_case_are_refused(self, tmp_path: Path) -> None:
        root = _app(tmp_path / "a")
        declared = {**packaged_inventory(root), "UI/index.js": "0" * 64}
        assert "differ only by case" in (inventory_mismatch(root, declared) or "")

    @pytest.mark.parametrize("digest", ["", "A" * 64, "0" * 63, 7])
    def test_a_malformed_digest_is_refused(self, tmp_path: Path, digest: object) -> None:
        root = _app(tmp_path / "a")
        declared: dict = dict(packaged_inventory(root))
        declared["ui/index.js"] = digest
        assert "SHA-256" in (inventory_mismatch(root, declared) or "")

    def test_a_non_object_is_refused(self, tmp_path: Path) -> None:
        assert inventory_mismatch(_app(tmp_path / "a"), ["ui/index.js"]) is not None


# ---------------------------------------------------------------------------
# The signed bytes
# ---------------------------------------------------------------------------

_BASE = {"name": "x", "version": "1", "signer": "s", "permissions": {}}


class TestSigningPayload:
    def test_is_the_tagged_whole_document_minus_the_signature(self) -> None:
        doc = {**_BASE, "signature": "abc", "displayName": "X", "futureKey": {"a": 1}}
        payload = json.loads(AppManifest.from_dict(doc).signing_payload())
        assert payload["scheme"] == APP_SIGNATURE_SCHEME == "kiro-crew-app/2"
        assert payload["manifest"] == {k: v for k, v in doc.items() if k != "signature"}

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("setup", {"onInstall": "id"}),
            ("backend", {"entryPoint": "evil.py"}),
            ("mcpServers", {"x": {"command": "evil"}}),
            ("dependencies", {"pip": ["evil"]}),
            ("displayName", "Impostor"),
            ("fileDigests", {"backend/server.py": "0" * 64}),
            ("anUnknownKey", True),
        ],
    )
    def test_every_key_is_covered(self, key: str, value: object) -> None:
        clean = AppManifest.from_dict(dict(_BASE))
        sig = hmac.new(b"k", clean.signing_payload(), hashlib.sha256).hexdigest()
        tampered = AppManifest.from_dict({**_BASE, "signature": sig, key: value})
        policy = AppAdmissionPolicy(require_signature=True, trust_keys={"s": "k"})
        assert tampered.signing_payload() != clean.signing_payload()
        assert not _signature_valid(tampered, policy)

    def test_listing_copy_stays_outside_the_signature(self) -> None:
        plain = AppManifest.from_dict(dict(_BASE)).signing_payload()
        translated = AppManifest.from_dict({**_BASE, "l10n": {"de": {"description": "x"}}})
        assert translated.signing_payload() == plain

    def test_a_scheme_1_signature_no_longer_verifies(self) -> None:
        legacy_bytes = json.dumps(_BASE, sort_keys=True, separators=(",", ":")).encode()
        legacy_sig = hmac.new(b"k", legacy_bytes, hashlib.sha256).hexdigest()
        manifest = AppManifest.from_dict({**_BASE, "signature": legacy_sig})
        policy = AppAdmissionPolicy(require_signature=True, trust_keys={"s": "k"})
        assert not _signature_valid(manifest, policy)

    def test_a_document_that_is_not_json_data_never_verifies(self) -> None:
        manifest = AppManifest.from_dict({**_BASE, "weight": float("nan"), "signature": "0"})
        policy = AppAdmissionPolicy(require_signature=True, trust_keys={"s": "k"})
        assert not _signature_valid(manifest, policy)


# ---------------------------------------------------------------------------
# Signing and admission
# ---------------------------------------------------------------------------


class TestSignAndAdmit:
    def test_a_signed_tree_is_admitted(self, tmp_path: Path, signing_policy: Path) -> None:
        root = _app(tmp_path / "src")
        doc = sign_app_dir(root, signer="acme", secret=SECRET)
        assert doc["fileDigests"] == packaged_inventory(root)
        manifest = AppManifest.from_json_file(root / "app.json")
        assert app_admission_denied("probe", manifest, action="install", app_root=root) is None

    def test_a_packaged_file_changed_after_signing_is_refused(
        self, tmp_path: Path, signing_policy: Path
    ) -> None:
        root = _app(tmp_path / "src")
        sign_app_dir(root, signer="acme", secret=SECRET)
        _write(root, "backend/server.py", "print('tampered')\n")
        manifest = AppManifest.from_json_file(root / "app.json")
        denied = app_admission_denied("probe", manifest, action="install", app_root=root)
        assert denied == (
            "app 'probe' files do not match its signature: "
            "backend/server.py does not match its signed digest"
        )

    def test_a_manifest_changed_after_signing_is_refused(
        self, tmp_path: Path, signing_policy: Path
    ) -> None:
        root = _app(tmp_path / "src")
        doc = sign_app_dir(root, signer="acme", secret=SECRET)
        doc["setup"] = {"onInstall": "id"}
        (root / "app.json").write_text(json.dumps(doc), encoding="utf-8")
        manifest = AppManifest.from_json_file(root / "app.json")
        denied = app_admission_denied("probe", manifest, action="install", app_root=root)
        assert denied == "app 'probe' signature invalid or unsigned"

    def test_an_activation_checks_the_installed_tree(
        self, tmp_path: Path, signing_policy: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installed = _app(tmp_path / "apps" / "probe")
        sign_app_dir(installed, signer="acme", secret=SECRET)
        monkeypatch.setattr(admission, "_installed_root", lambda name: tmp_path / "apps" / name)
        monkeypatch.setattr(admission, "app_version_refusal", lambda *a, **k: None)
        manifest = AppManifest.from_json_file(installed / "app.json")
        assert app_admission_denied("probe", manifest, action="enable") is None
        _write(installed, "backend/server.py", "print('tampered')\n")
        for action in ("boot", "enable", "restart"):
            assert "files do not match" in (
                app_admission_denied("probe", manifest, action=action) or ""
            )

    def test_a_check_with_no_tree_is_the_signature_alone(
        self, tmp_path: Path, signing_policy: Path
    ) -> None:
        root = _app(tmp_path / "src")
        sign_app_dir(root, signer="acme", secret=SECRET)
        _write(root, "backend/server.py", "print('tampered')\n")
        manifest = AppManifest.from_json_file(root / "app.json")
        # The registry's pre-clone check holds a manifest only; the clone-time
        # check, with the tree, is where the file is caught.
        assert app_admission_denied("probe", manifest, action="install_from_registry") is None

    def test_no_signature_policy_never_reads_the_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(admission, "config_dir", lambda: home)

        def _boom(*_a: object, **_k: object) -> None:
            raise AssertionError("hashed a tree with no signature policy")

        monkeypatch.setattr(admission, "inventory_mismatch", _boom)
        root = _app(tmp_path / "src")
        manifest = AppManifest.from_json_file(root / "app.json")
        assert app_admission_denied("probe", manifest, action="install", app_root=root) is None
        assert installed_content_refusal("probe", root) is None


class TestInstalledContentRefusal:
    @pytest.fixture
    def installed(
        self, tmp_path: Path, signing_policy: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        root = _app(tmp_path / "apps" / "probe")
        sign_app_dir(root, signer="acme", secret=SECRET)
        monkeypatch.setattr(admission, "_installed_root", lambda name: tmp_path / "apps" / name)
        monkeypatch.setattr(
            "kiro_crew.apps.manager.get_app_manifest",
            lambda name: AppManifest.from_json_file(tmp_path / "apps" / name / "app.json"),
        )
        return root

    def test_the_signed_installed_tree_runs(self, installed: Path) -> None:
        assert installed_content_refusal("probe", installed / "backend" / "server.py") is None

    def test_a_file_changed_after_install_does_not_run(self, installed: Path) -> None:
        _write(installed, "backend/server.py", "print('tampered')\n")
        assert installed_content_refusal("probe", installed / "backend" / "server.py") == (
            "app 'probe' files do not match its signature: "
            "backend/server.py does not match its signed digest"
        )

    def test_an_installed_manifest_edit_does_not_run(self, installed: Path) -> None:
        doc = json.loads((installed / "app.json").read_text())
        doc["setup"] = {"onEnable": "id"}
        (installed / "app.json").write_text(json.dumps(doc))
        assert installed_content_refusal("probe", installed) == (
            "app 'probe' signature invalid or unsigned"
        )

    def test_the_manifest_being_executed_is_the_one_verified(self, installed: Path) -> None:
        doc = json.loads((installed / "app.json").read_text())
        doc["backend"]["entryPoint"] = "ui/index.js"  # what this caller would run
        held = AppManifest.from_dict(doc)  # the app.json on disk is still the signed one
        assert installed_content_refusal("probe", installed, held) == (
            "app 'probe' signature invalid or unsigned"
        )
        assert installed_content_refusal("probe", installed) is None

    def test_a_lifecycle_script_must_be_one_the_signed_manifest_declares(
        self, installed: Path
    ) -> None:
        assert installed_content_refusal("probe", installed, None, "rm -rf ~") == (
            "app 'probe' script is not one its signed manifest declares"
        )

    def test_code_outside_the_installed_tree_is_not_judged(
        self, installed: Path, tmp_path: Path
    ) -> None:
        _write(installed, "backend/server.py", "print('tampered')\n")
        assert installed_content_refusal("probe", tmp_path / "shipped" / "pkg") is None


# ---------------------------------------------------------------------------
# Each executable surface asks the check before it runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lifecycle_script_does_not_run_from_a_changed_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.apps import lifecycle_scripts

    (tmp_path / "demo-app").mkdir()
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    seen: list[tuple[str, Path]] = []

    def _refuse(name: str, executed: Path, *_rest: object) -> str:
        seen.append((name, executed))
        return "files do not match its signature"

    monkeypatch.setattr(lifecycle_scripts, "_installed_content_refusal", _refuse)

    async def _unexpected(*a: object, **k: object) -> None:
        pytest.fail("spawned a lifecycle script from a tree that fails its signature")

    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _unexpected)
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "echo hi")
    assert result == {"output": "files do not match its signature", "failed": True, "denied": True}
    assert seen == [("demo-app", tmp_path / "demo-app")]


def test_a_backend_does_not_spawn_from_a_changed_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.apps import backend as bmod

    root = tmp_path / "app-root"
    _write(root, "backend/server.py", "print('x')\n")
    monkeypatch.setattr(bmod, "app_dir", lambda _name: root)
    monkeypatch.setattr(bmod, "app_execution_denied", lambda _name, **_kw: None)
    seen: list[Path] = []

    def _refuse(name: str, executed: Path, *_rest: object) -> str:
        seen.append(executed)
        return "files do not match its signature"

    monkeypatch.setattr(bmod, "_installed_content_refusal", _refuse)

    def _unexpected(*a: object, **k: object) -> None:
        pytest.fail("spawned a backend from a tree that fails its signature")

    monkeypatch.setattr(bmod, "wrap_argv", _unexpected)
    manifest = AppManifest.from_dict(
        {"name": "probe", "version": "1", "backend": {"entryPoint": "backend/server.py"}}
    )
    assert bmod._start_app_backend_body("probe", manifest) is None
    assert seen == [root / "backend" / "server.py"]


def test_a_hook_module_does_not_load_from_a_changed_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.apps import module_loader

    root = tmp_path / "probe"
    _write(root, "hooks/main.py", "def register(ctx):\n    return 1\n")
    monkeypatch.setattr(module_loader, "_is_builtin_app", lambda *a: False)
    monkeypatch.setattr(module_loader, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        module_loader,
        "_verified_installed_manifest",
        lambda name, executed: ("files do not match its signature", None),
    )
    with pytest.raises(ImportError, match="files do not match its signature"):
        module_loader.load_app_module("probe", root, "hooks.main:register")


def test_resources_do_not_register_from_a_changed_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.apps import bridges, bridges_sources

    monkeypatch.setattr(bridges_sources, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        bridges,
        "_installed_content_refusal",
        lambda *_a, **_k: "files do not match its signature",
    )
    denied = bridges_sources._registration_denied("probe", action="mcp_register", app_root=tmp_path)
    assert denied == "files do not match its signature"


# ---------------------------------------------------------------------------
# The publisher's command
# ---------------------------------------------------------------------------


def test_kirocrew_app_sign_writes_a_signature_admission_accepts(
    tmp_path: Path, signing_policy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from kiro_crew.cli_commands_app import _handle_app_sign

    root = _app(tmp_path / "src")
    key = tmp_path / "key"
    key.write_text(SECRET + "\n")
    _handle_app_sign(argparse.Namespace(source=str(root), signer="acme", key_file=str(key)))
    assert "Signed probe as 'acme' (2 packaged files)" in capsys.readouterr().out
    doc = json.loads((root / "app.json").read_text())
    assert doc["signer"] == "acme" and set(doc["fileDigests"]) == {
        "backend/server.py",
        "ui/index.js",
    }
    manifest = AppManifest.from_json_file(root / "app.json")
    assert app_admission_denied("probe", manifest, action="install", app_root=root) is None


def test_kirocrew_app_sign_refuses_a_tree_with_a_link(tmp_path: Path) -> None:
    from kiro_crew.cli_commands_app import _handle_app_sign

    root = _app(tmp_path / "src")
    make_dir_link(root / "ui" / "escape", tmp_path)
    key = tmp_path / "key"
    key.write_text(SECRET)
    with pytest.raises(SystemExit):
        _handle_app_sign(argparse.Namespace(source=str(root), signer="acme", key_file=str(key)))
    assert "signature" not in json.loads((root / "app.json").read_text())


def test_signed_content_and_the_copy_share_one_skip_rule(tmp_path: Path) -> None:
    from kiro_crew.apps import manager, manager_tree

    assert manager.is_uncopied_name is manager_tree.is_uncopied_name
    root = _app(tmp_path / "a")
    _write(root, "ui/.kirocrew-deps-staging-123-0123abcd/x.py", "x")
    assert ".kirocrew-deps-staging" not in "".join(packaged_inventory(root))
    for name in ("node_modules", "__pycache__", ".git", ".kirocrew-deps", "installed.json"):
        assert manager_tree.is_uncopied_name(name)
    assert not manager_tree.is_uncopied_name("backend")


# ---------------------------------------------------------------------------
# Crons of a signed app come from its signed manifest
# ---------------------------------------------------------------------------


def test_a_signed_app_with_crons_installs_enables_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import bridges_crons
    from kiro_crew.apps.bridges import register_app
    from kiro_crew.apps.manager import app_dir, enable_app, get_app_manifest, install_app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(json.dumps({"agent": {"apps_allow_third_party": True}}))
    (home / "app_admission.json").write_text(
        json.dumps({"mode": "enforce", "require_signature": True, "trust_keys": {"acme": SECRET}})
    )
    src = tmp_path / "src" / "cron-probe"
    src.mkdir(parents=True)
    (src / "app.json").write_text(
        json.dumps(
            {
                "name": "cron-probe",
                "version": "1.0.0",
                "displayName": "Cron probe",
                "description": "a signed app with a cron",
                "permissions": {"cron": True},
                "crons": [{"name": "refresh", "every": 3600, "message": "go"}],
            }
        )
    )
    _write(src, "notes.md", "published\n")
    sign_app_dir(src, signer="acme", secret=SECRET)

    assert install_app(src).ok
    assert enable_app("cron-probe").ok
    assert register_app("cron-probe").crons  # writes the cron record beside the app
    installed = app_dir("cron-probe")
    assert (installed / "app-crons.json").is_file()
    assert installed_content_refusal("cron-probe", installed) is None
    # A gateway restart: the activation and the cron promotion are both admitted.
    manifest = get_app_manifest("cron-probe")
    assert app_admission_denied("cron-probe", manifest, action="boot") is None
    defs, _root = bridges_crons._admitted_app_cron_defs("cron-probe")
    assert [d["message"] for d in defs] == ["go"]
    # The record is not signed, so under the policy it never decides what runs.
    record = json.loads((installed / "app-crons.json").read_text())
    record[0]["message"] = "rewritten"
    (installed / "app-crons.json").write_text(json.dumps(record))
    defs, _root = bridges_crons._admitted_app_cron_defs("cron-probe")
    assert [d["message"] for d in defs] == ["go"]


# ---------------------------------------------------------------------------
# A fresh registry clone holds only what the signature covers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "planted",
    [
        "node_modules/left-pad/index.js",
        ".venv/lib/site.pth",
        "backend/__pycache__/server.cpython-312.pyc",
        ".kirocrew-deps/evil.py",
        "data/seed.json",
        "app-crons.json",
    ],
)
def test_a_fresh_clone_holding_unsigned_content_is_refused(
    tmp_path: Path, signing_policy: Path, planted: str
) -> None:
    root = _app(tmp_path / "clone")
    sign_app_dir(root, signer="acme", secret=SECRET)
    _write(root, planted, "x")
    manifest = AppManifest.from_json_file(root / "app.json")
    denied = app_admission_denied(
        "probe", manifest, action="install_from_registry", app_root=root, fresh_checkout=True
    )
    first_left_out = "backend/__pycache__" if "__pycache__" in planted else planted.split("/")[0]
    assert (denied or "").endswith(f"cannot cover: {first_left_out}")


def test_a_fresh_clone_keeps_its_own_git_directory(tmp_path: Path, signing_policy: Path) -> None:
    root = _app(tmp_path / "clone")
    sign_app_dir(root, signer="acme", secret=SECRET)
    _write(root, ".git/HEAD", "ref: refs/heads/main\n")
    manifest = AppManifest.from_json_file(root / "app.json")
    assert (
        app_admission_denied(
            "probe", manifest, action="install_from_registry", app_root=root, fresh_checkout=True
        )
        is None
    )


# ---------------------------------------------------------------------------
# Builtin provenance comes from the executed path, never the installed record
# ---------------------------------------------------------------------------


def test_the_installed_record_cannot_claim_the_builtin_exemption(
    tmp_path: Path, signing_policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _app(tmp_path / "apps" / "probe")
    sign_app_dir(root, signer="acme", secret=SECRET)
    _write(root, "backend/server.py", "print('tampered')\n")
    monkeypatch.setattr(admission, "_installed_root", lambda name: tmp_path / "apps" / name)
    monkeypatch.setattr("kiro_crew.apps.manager.builtin_owns_installed", lambda name: True)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest",
        lambda name: AppManifest.from_json_file(root / "app.json"),
    )
    assert installed_content_refusal("probe", root / "backend" / "server.py") is not None
    monkeypatch.setattr("kiro_crew.apps.execution.is_builtin_app", lambda **_k: True)
    assert installed_content_refusal("probe", root / "backend" / "server.py") is None


# ---------------------------------------------------------------------------
# No bytecode from a signed app's own tree runs
# ---------------------------------------------------------------------------


def _plant_bytecode(source_file: Path, evil_source: str, scratch: Path) -> None:
    """An unchecked-hash .pyc beside *source_file*, which CPython runs without the source."""
    import importlib.util
    import py_compile

    evil = scratch / (source_file.stem + "_evil.py")
    evil.write_text(evil_source)
    py_compile.compile(
        str(evil),
        cfile=importlib.util.cache_from_source(str(source_file)),
        dfile=str(source_file),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )


def _hook_app(root: Path) -> Path:
    root = _app(root)
    _write(root, "hooks/__init__.py", "")
    _write(root, "hooks/helper.py", "VALUE = 'source'\n")
    _write(
        root,
        "hooks/main.py",
        "from . import helper\n\ndef register(ctx=None):\n    return helper.VALUE\n",
    )
    return root


@pytest.mark.parametrize("planted", ["main", "helper"])
def test_planted_bytecode_beside_a_signed_hook_does_not_run(
    tmp_path: Path, signing_policy: Path, monkeypatch: pytest.MonkeyPatch, planted: str
) -> None:
    from kiro_crew.apps import module_loader

    name = f"pyc-probe-{planted}"
    root = _hook_app(tmp_path / "apps" / name)
    sign_app_dir(root, signer="acme", secret=SECRET)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    evil = (
        "def register(ctx=None):\n    return 'bytecode'\n"
        if planted == "main"
        else "VALUE = 'bytecode'\n"
    )
    _plant_bytecode(root / "hooks" / f"{planted}.py", evil, scratch)
    monkeypatch.setattr(admission, "_installed_root", lambda n: tmp_path / "apps" / n)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest",
        lambda n: AppManifest.from_json_file(tmp_path / "apps" / n / "app.json"),
    )
    monkeypatch.setattr(module_loader, "_is_builtin_app", lambda *a: False)
    monkeypatch.setattr(module_loader, "app_execution_denied", lambda *a, **k: None)
    try:
        assert module_loader.load_app_module(name, root, "hooks.main:register")() == "source"
    finally:
        module_loader.unload_app_modules(name)
    # Control: with no signature policy the planted bytecode is what CPython runs.
    _plant_bytecode(root / "hooks" / f"{planted}.py", evil, scratch)
    (signing_policy / "app_admission.json").unlink()
    try:
        assert module_loader.load_app_module(name, root, "hooks.main:register")() == "bytecode"
    finally:
        module_loader.unload_app_modules(name)


def test_a_signed_apps_python_child_gets_a_cache_prefix_outside_its_tree(
    tmp_path: Path, signing_policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from kiro_crew.apps.signed_imports import pycache_prefix_env

    root = _app(tmp_path / "apps" / "probe")
    sign_app_dir(root, signer="acme", secret=SECRET)
    monkeypatch.setattr(admission, "_installed_root", lambda n: tmp_path / "apps" / n)
    prefix = pycache_prefix_env("probe", root / "backend" / "server.py")
    assert set(prefix) == {"PYTHONPYCACHEPREFIX"}
    where = Path(prefix["PYTHONPYCACHEPREFIX"])
    assert where.is_dir() and not any(where.iterdir()) and not where.is_relative_to(root)
    out = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.pycache_prefix)"],
        env={**os.environ, **prefix},
        capture_output=True,
        encoding="utf-8",
    )
    assert out.stdout.strip() == str(where)
    # Outside the installed tree, or with no signature policy, nothing is set.
    assert pycache_prefix_env("probe", tmp_path / "elsewhere" / "x.py") == {}
    (signing_policy / "app_admission.json").unlink()
    assert pycache_prefix_env("probe", root / "backend" / "server.py") == {}


def test_the_backend_spawn_hands_its_child_the_gateway_pycache_prefix(
    spawn_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps_backend_coverage_helpers import _capture_popen, _manifest, _StopSpawn

    from kiro_crew.apps import backend as bmod

    (spawn_root / "server.py").write_text("x = 1\n")
    monkeypatch.setattr(
        bmod, "_pycache_prefix_env", lambda name, executed: {"PYTHONPYCACHEPREFIX": "/gw/pyc"}
    )
    seen = _capture_popen(monkeypatch)
    with pytest.raises(_StopSpawn):
        bmod._start_app_backend_body("probe", _manifest("server.py"))
    assert seen["kwargs"]["env"]["PYTHONPYCACHEPREFIX"] == "/gw/pyc"


@pytest.mark.asyncio
async def test_a_lifecycle_script_gets_the_gateway_pycache_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew import platform_compat
    from kiro_crew.apps import lifecycle_scripts

    (tmp_path / "demo-app").mkdir()
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle_scripts, "_installed_content_refusal", lambda *a: None)
    monkeypatch.setattr(lifecycle_scripts, "wrap_argv", lambda argv, **k: (argv, None))
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(lifecycle_scripts.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(
        lifecycle_scripts, "_pycache_prefix_env", lambda n, p: {"PYTHONPYCACHEPREFIX": "/gw/pyc"}
    )
    seen: dict = {}

    class _Proc:
        pid, returncode = 4242, 0

        async def communicate(self):
            return b"ok", None

    async def _spawn(*argv, **kwargs):
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _spawn)
    assert platform_compat is not None
    await lifecycle_scripts.run_lifecycle_script("demo-app", "echo hi")
    assert seen["env"]["PYTHONPYCACHEPREFIX"] == "/gw/pyc"
