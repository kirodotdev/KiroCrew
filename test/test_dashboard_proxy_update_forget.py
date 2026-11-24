"""An update that does not re-seal the install record ends the ``dashboard_proxy`` grant.

The grant names the sealed install record (``install_identity``). A folder update
(``update_app`` from a local path) replaces the app's code but never seals a new record,
so the old record, and the grant that names it, would otherwise keep standing for code
the owner never saw. ``update_app`` drops the record before the tree swap, under the same
lifecycle lock as the rest of the update and before the route restarts the backend. A
registry update re-seals right after (``set_app_provenance``) with a new identity.
"""

from __future__ import annotations

import json

import pytest
from app_manager_helpers import _make_app_source, app_home_fixture  # noqa: F401 - fixture

from kiro_crew.apps import dashboard_proxy
from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps.manager import enable_app, get_app, get_app_manifest, install_app, update_app

_APP = "test-app"


@pytest.fixture
def sealed(app_home, tmp_path, monkeypatch):
    """A registry-sealed install of ``test-app`` the owner granted ``dashboard_proxy``."""
    monkeypatch.setattr(
        "kiro_crew.member_records.records_dir",
        lambda kind: app_home / "member-records" / kind,
    )
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, True))
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: True)
    assert install_app(_make_app_source(tmp_path)).ok
    # Enabled: a write that leaves the app disabled revokes every grant on its own
    # (``_write_installed``), which would hide what these tests pin.
    assert enable_app(_APP).ok
    assert dashboard_proxy.record_install(
        _APP, version="1.0.0", registry="crew-tunnels", url="https://example.invalid/t.git"
    )
    identity = dashboard_proxy.install_identity(_APP)
    assert dashboard_proxy.grant(_APP, identity) == (True, "")
    assert dashboard_proxy.granted(_APP) is True
    return identity


def _next_version(tmp_path, version: str = "2.0.0"):
    src = tmp_path / "next" / _APP
    src.mkdir(parents=True)
    manifest = json.loads(
        (tmp_path / "source" / _APP / manager_mod.APP_MANIFEST_FILENAME).read_text()
    )
    manifest["version"] = version
    (src / manager_mod.APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    (src / "new-code.txt").write_text("code the owner never saw", encoding="utf-8")
    return src


def test_a_folder_update_ends_the_grant_and_the_offer(sealed, tmp_path):
    result = update_app(_next_version(tmp_path), expected_name=_APP)
    assert result.ok, result.error
    assert get_app_manifest(_APP).version == "2.0.0"
    assert get_app(_APP)["enabled"] is True, "the update kept the app enabled"
    assert dashboard_proxy.granted(_APP) is False, "the grant named the code before the update"
    assert dashboard_proxy.prompt_text(_APP) is None, "a folder update is not a registry install"
    assert dashboard_proxy.install_identity(_APP) is None


def test_a_registry_update_reseals_with_an_identity_the_old_grant_does_not_match(sealed, tmp_path):
    result = update_app(
        _next_version(tmp_path),
        expected_name=_APP,
        source_repository="https://example.invalid/t.git",
    )
    assert result.ok, result.error
    # The registry path seals right after update_app (set_app_provenance -> record_install).
    assert dashboard_proxy.record_install(
        _APP, version="2.0.0", registry="crew-tunnels", url="https://example.invalid/t.git"
    )
    renewed = dashboard_proxy.install_identity(_APP)
    assert renewed and renewed != sealed
    assert dashboard_proxy.granted(_APP) is False
    assert dashboard_proxy.prompt_text(_APP) is not None, "the owner is offered the new install"
    assert dashboard_proxy.grant(_APP, sealed) == (False, "offer_changed")


def test_a_failed_update_never_leaves_the_grant_beside_new_code(sealed, tmp_path, monkeypatch):
    def _copy_fails(_source, _dest):
        raise OSError("disk full")

    monkeypatch.setattr(manager_mod, "_copy_app_tree", _copy_fails)
    result = update_app(_next_version(tmp_path), expected_name=_APP)
    assert not result.ok
    assert get_app_manifest(_APP).version == "1.0.0", "the rollback restores the old tree"
    assert not (manager_mod.app_dir(_APP) / "new-code.txt").exists()
    # Fail closed: the record is dropped before the swap, so no rollback path can leave
    # the grant standing beside new code. The owner grants again after a good update.
    assert dashboard_proxy.granted(_APP) is False


def test_an_update_refused_before_anything_changes_keeps_the_grant(sealed, tmp_path):
    result = update_app(_next_version(tmp_path), expected_name="another-app")
    assert not result.ok
    assert dashboard_proxy.granted(_APP) is True
