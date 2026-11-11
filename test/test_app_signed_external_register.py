"""A signed self-registered app activates; the gateway holds none of its files.

``register_external_app`` stores only metadata (``app.json``, records, the app
secret and ``data``) in the installed directory, while the app's packaged files
live and run outside the gateway. Activation must not compare that empty
directory with ``fileDigests``, yet anything the gateway would execute from it
is still checked against the signature.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_app_signed_round3 import sign
from test_app_signed_round3 import signed_home as _signed_home
from test_app_signed_round3 import write_app

from kiro_crew.apps import admission, manager
from kiro_crew.apps.manager_records import InstalledApp

signed_home = _signed_home


def _signed_external(tmp_path: Path) -> dict:
    """A signed app tree outside the gateway, as ``kirocrew app sign`` leaves it."""
    tree = tmp_path / "external-app"
    write_app(tree)
    (tree / "server.py").write_text("print('runs outside the gateway')\n", encoding="utf-8")
    sign(tree)
    document = json.loads((tree / "app.json").read_text(encoding="utf-8"))
    assert "server.py" in document["fileDigests"]
    return document


def _register(tmp_path: Path, document: dict) -> Path:
    result = manager.register_external_app(
        "probe",
        "1.0.0",
        "Probe",
        source=str(tmp_path / "external-app"),
        manifest_data=document,
        version_checked=True,
    )
    assert result.ok, result.error
    return manager.app_dir("probe")


@pytest.mark.parametrize("action", ["boot", "enable", "restart"])
def test_signed_external_registration_activates(
    signed_home: Path, tmp_path: Path, action: str
) -> None:
    metadata = _register(tmp_path, _signed_external(tmp_path))
    manifest = manager.get_app_manifest("probe")
    assert manifest is not None
    refusal = admission.app_admission_denied("probe", manifest, action, check_version=False)
    assert refusal is None
    assert not (metadata / "server.py").exists()


def test_external_metadata_directory_never_runs_unverified(
    signed_home: Path, tmp_path: Path
) -> None:
    metadata = _register(tmp_path, _signed_external(tmp_path))
    # Code the gateway would execute from the metadata directory is still bound
    # to the signature: the signed files are not there, so nothing runs.
    refusal = admission.installed_content_refusal("probe", metadata)
    assert refusal is not None and "server.py" in refusal


def test_file_planted_in_external_metadata_directory_is_refused(
    signed_home: Path, tmp_path: Path
) -> None:
    metadata = _register(tmp_path, _signed_external(tmp_path))
    (metadata / "planted.py").write_text("print('unsigned')\n", encoding="utf-8")
    manifest = manager.get_app_manifest("probe")
    refusal = admission.app_admission_denied("probe", manifest, "boot", check_version=False)
    assert refusal is not None and admission._is_signature_refusal(refusal)


def test_gateway_managed_record_without_files_is_still_refused(signed_home: Path) -> None:
    root = signed_home / "apps" / "probe"
    write_app(root)
    (root / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = sign(root)
    (root / "helper.py").unlink()
    meta = InstalledApp(name="probe", resources="gateway", source=str(root))
    (root / "installed.json").write_text(json.dumps(meta.to_dict()), encoding="utf-8")
    refusal = admission.app_admission_denied("probe", manifest, "boot", check_version=False)
    assert refusal is not None and admission._is_signature_refusal(refusal)
    assert "helper.py" in refusal


@pytest.mark.parametrize("action", ["boot", "enable"])
def test_without_policy_external_registration_activates(
    signed_home: Path, tmp_path: Path, action: str
) -> None:
    (signed_home / "app_admission.json").unlink()
    _register(tmp_path, _signed_external(tmp_path))
    manifest = manager.get_app_manifest("probe")
    assert admission.app_admission_denied("probe", manifest, action, check_version=False) is None
