"""Focused contract tests for device-bound mobile SSH gateway tokens."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew import mobile_ssh as ms
from kiro_crew import mobile_ssh_cli as mobile_cli
from kiro_crew.dashboard import token_auth as ta
from kiro_crew.dashboard.handlers import mobile_ssh as mobile_handlers
from kiro_crew.dashboard.refresh_tokens import refresh_cookie_name
from kiro_crew.dashboard.routes import register_all
from kiro_crew.dashboard.server import _register_mcp_routes
from kiro_crew.platform.defaults import DefaultMobileConnectProvider


def _ssh_string(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def _public_key(seed: int = 1) -> str:
    blob = _ssh_string(b"ssh-ed25519") + _ssh_string(bytes([seed]) * 32)
    return f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')} phone"


def _binding(line: str) -> str:
    match = re.search(r"--binding ([0-9a-f]{64})", line)
    assert match is not None
    return match.group(1)


@pytest.fixture(autouse=True)
def _host_owned_local_caller(monkeypatch):
    monkeypatch.setattr(mobile_handlers, "local_owner_bootstrap_allowed", lambda _request: True)
    monkeypatch.setattr(mobile_handlers, "mint_denied_reason", lambda _method: "")


@pytest.fixture
def mobile_store(tmp_path, monkeypatch):
    import kiro_crew.dashboard.revocation_gen as revocation_gen

    store = ms.MobileSshDeviceStore(tmp_path)
    monkeypatch.setattr(ms, "_store_singleton", store)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    ta._state.clear_all()
    yield store
    ta._state.clear_all()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("crew.example.com", "crew.example.com"),
        ("CREW.INTERNAL", "crew.internal"),
        ("192.168.40.12", "192.168.40.12"),
        ("my-mac.local", "my-mac.local"),
        ("devbox.lan", "devbox.lan"),
    ],
)
def test_generic_ssh_hosts_are_accepted(raw: str, expected: str) -> None:
    assert ms.validate_ssh_host_descriptor(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "localhost",
        "api.localhost",
        "127.0.0.1",
        "::1",
        "2001:db8::1",
        "[2001:db8::1]",
        "169.254.10.1",
        "0.0.0.0",
        "224.0.0.1",
        "user@host",
        "ssh://host",
        "host;id",
        "host name",
        "-bad.example",
        "bad..example",
    ],
)
def test_unsafe_or_malformed_ssh_hosts_are_rejected(raw: str) -> None:
    with pytest.raises(ms.MobileSshError) as exc:
        ms.validate_ssh_host_descriptor(raw)
    assert exc.value.code == "invalid_ssh_host"


@pytest.mark.parametrize("device_id", ["phone", "1phone", "9", "phone-17-pro"])
def test_device_id_accepts_documented_leading_digits(device_id: str) -> None:
    assert ms._validate_device_id(device_id) == device_id


@pytest.mark.parametrize("device_id", ["Phone", "-phone", "phone_1", "phone.", "éphone"])
def test_device_id_rejects_other_spellings(device_id: str) -> None:
    with pytest.raises(ms.MobileSshError) as exc:
        ms._validate_device_id(device_id)
    assert exc.value.code == "invalid_device_id"


def test_public_key_validation_is_structural() -> None:
    canonical, digest, fingerprint = ms.validate_ed25519_public_key(_public_key())
    assert canonical.startswith("ssh-ed25519 ")
    assert len(digest) == 64
    assert fingerprint.startswith("SHA256:")
    with pytest.raises(ms.MobileSshError):
        ms.validate_ed25519_public_key("ssh-ed25519 not-base64")
    with pytest.raises(ms.MobileSshError):
        ms.validate_ed25519_public_key("ssh-rsa AAAA")


@pytest.mark.skipif(os.name == "nt", reason="POSIX OpenSSH host-key source contract")
def test_host_public_key_source_is_regular_protected_and_public(tmp_path) -> None:
    path = tmp_path / "ssh_host_ed25519_key.pub"
    path.write_text(_public_key(), encoding="ascii")
    path.chmod(0o644)
    assert ms._read_verified_host_public_key(path, expected_owner_uid=os.getuid()) == _public_key()
    path.chmod(0o666)
    with pytest.raises(ms.MobileSshError) as exc:
        ms._read_verified_host_public_key(path, expected_owner_uid=os.getuid())
    assert exc.value.code == "host_key_untrusted"
    assert all(path.name.endswith(".pub") for path in ms._HOST_PUBLIC_KEY_PATHS)


@pytest.mark.skipif(os.name == "nt", reason="POSIX OpenSSH host-key source contract")
def test_host_identity_returns_username_and_pin(monkeypatch) -> None:
    monkeypatch.setattr(ms, "_current_username", lambda: "crewuser")
    monkeypatch.setattr(ms, "_read_verified_host_public_key", lambda _path: _public_key())
    identity = ms.discover_host_ssh_identity()
    assert identity.username == "crewuser"
    assert identity.host_key_algorithm == "ssh-ed25519"
    assert identity.host_key_fingerprint.startswith("SHA256:")


def test_windows_host_identity_fails_actionably(monkeypatch) -> None:
    monkeypatch.setattr(ms.platform_compat, "IS_WINDOWS", True)
    with pytest.raises(ms.MobileSshError) as exc:
        ms.discover_host_ssh_identity()
    assert exc.value.code == "platform_unsupported"
    assert "Linux or macOS" in exc.value.message


def test_enroll_persists_only_hashes_and_emits_restricted_line(mobile_store, tmp_path) -> None:
    result = mobile_store.enroll("1phone", _public_key(), label="Phone", launcher=sys.executable)
    line = result.authorized_keys_line
    for restriction in (
        "restrict",
        'port-forwarding,permitopen="127.0.0.1:5476",',
        "no-pty",
        "no-agent-forwarding",
        "no-X11-forwarding",
        "no-user-rc",
        'command="',
    ):
        assert restriction in line
    assert ms.MOBILE_ORIGINAL_COMMAND not in line
    assert " mobile ssh token " in line
    # permitlisten="none" fails the whole key, so -R is pinned to privileged loopback port 1.
    assert 'permitlisten="127.0.0.1:1",' in line
    assert 'permitlisten="none"' not in line
    persisted = (tmp_path / ms.MOBILE_SSH_DIR / ms.MOBILE_SSH_STORE).read_text()
    binding = _binding(line)
    assert _public_key().split()[1] not in persisted
    assert binding not in persisted
    assert "PRIVATE" not in persisted
    loaded = ms.MobileSshDeviceStore(tmp_path)
    assert loaded.list_devices() == mobile_store.list_devices()


def test_enroll_binds_forced_command_and_forward_to_gateway_port(mobile_store) -> None:
    line = mobile_store.enroll(
        "phone", _public_key(), launcher=sys.executable, gateway_port=5477
    ).authorized_keys_line
    assert 'permitopen="127.0.0.1:5477",' in line
    assert " --port 5477" in line
    assert "5476" not in line


def test_enroll_pins_the_gateway_data_home_in_the_forced_command(mobile_store, tmp_path) -> None:
    line = mobile_store.enroll("phone", _public_key(), launcher=sys.executable).authorized_keys_line
    home_arg = shlex.join(["--home", str(tmp_path.resolve())])
    assert " " + home_arg.replace("\\", "\\\\").replace('"', '\\"') in line


@pytest.mark.parametrize("port", [0, 65536])
def test_enroll_rejects_out_of_range_gateway_port(mobile_store, port) -> None:
    with pytest.raises(ms.MobileSshError) as exc:
        mobile_store.enroll("phone", _public_key(), launcher=sys.executable, gateway_port=port)
    assert exc.value.code == "invalid_port"
    assert mobile_store.list_devices() == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_registry_and_lock_are_owner_only(mobile_store, tmp_path) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    root = tmp_path / ms.MOBILE_SSH_DIR
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / ms.MOBILE_SSH_STORE).stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "devices.lock").stat().st_mode) == 0o600


def test_duplicate_device_and_duplicate_key_are_refused(mobile_store) -> None:
    mobile_store.enroll("phone-a", _public_key(1), launcher=sys.executable)
    with pytest.raises(ms.MobileSshError) as same_id:
        mobile_store.enroll("phone-a", _public_key(2), launcher=sys.executable)
    assert same_id.value.code == "device_exists"
    with pytest.raises(ms.MobileSshError) as same_key:
        mobile_store.enroll("phone-b", _public_key(1), launcher=sys.executable)
    assert same_key.value.code == "public_key_exists"


def test_binding_mismatch_and_revocation_fail_closed(mobile_store) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    binding = _binding(enrolled.authorized_keys_line)
    with pytest.raises(ms.MobileSshError) as mismatch:
        mobile_store.mint("phone", "0" * 64)
    assert mismatch.value.code == "key_binding_failed"
    token = str(mobile_store.mint("phone", binding)["token"])
    assert ta.validate_token(token)[0] is True
    mobile_store.revoke("phone")
    valid, _user, reason = ta.validate_token(token)
    assert valid is False
    assert reason == "mobile device revoked"
    with pytest.raises(ms.MobileSshError) as revoked:
        mobile_store.mint("phone", binding)
    assert revoked.value.code == "device_revoked"


def test_reenrollment_replaces_old_binding_and_enrollment(mobile_store) -> None:
    first = mobile_store.enroll("phone", _public_key(1), launcher=sys.executable)
    old_binding = _binding(first.authorized_keys_line)
    mobile_store.revoke("phone")
    second = mobile_store.enroll("phone", _public_key(2), launcher=sys.executable)
    assert second.device.enrollment_id != first.device.enrollment_id
    with pytest.raises(ms.MobileSshError) as old:
        mobile_store.mint("phone", old_binding)
    assert old.value.code == "key_binding_failed"


def _write_registry(tmp_path, payload: object) -> Path:
    path = tmp_path / ms.MOBILE_SSH_DIR / ms.MOBILE_SSH_STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def _record(device_id: str = "phone", **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "device_id": device_id,
        "label": "",
        "key_sha256": "ab" * 32,
        "ssh_fingerprint": "SHA256:" + "A" * 43,
        "enrollment_id": "cd" * 16,
        "binding_sha256": "ef" * 32,
        "enrolled_at": "2026-01-01T00:00:00Z",
        "revoked_at": "",
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "dict"],
        {"version": 1, "devices": [_record(), _record()]},
        {"version": 1, "devices": [_record(device_id="Phone")]},
        {"version": 1, "devices": [_record(revoked_at=None)]},
        {"version": 1, "devices": [{**_record(), "extra": "x"}]},
        {"version": 1, "devices": [{k: v for k, v in _record().items() if k != "label"}]},
        {"version": 1, "devices": ["phone"]},
    ],
)
def test_malformed_registry_fails_closed_as_unavailable(tmp_path, payload) -> None:
    _write_registry(tmp_path, payload)
    with pytest.raises(ms.MobileSshError) as exc:
        ms.MobileSshDeviceStore(tmp_path)
    assert exc.value.code == "store_unavailable"
    assert exc.value.status == 503


def test_registry_changes_by_another_process_are_honored(tmp_path) -> None:
    writer = ms.MobileSshDeviceStore(tmp_path)
    reader = ms.MobileSshDeviceStore(tmp_path)
    enrolled = writer.enroll("phone", _public_key(), launcher=sys.executable)
    binding = _binding(enrolled.authorized_keys_line)
    token = str(reader.mint("phone", binding)["token"])
    claims = json.loads(ta._b64url_decode(token.split(".", 1)[0]))
    assert reader.validate_claims(claims) == (True, "")
    writer.revoke("phone")
    assert reader.validate_claims(claims) == (True, "")
    reader.refresh()
    assert reader.validate_claims(claims) == (False, "mobile device revoked")
    with pytest.raises(ms.MobileSshError) as revoked:
        reader.mint("phone", binding)
    assert revoked.value.code == "device_revoked"


def test_list_devices_reports_another_process_revocation(tmp_path) -> None:
    writer = ms.MobileSshDeviceStore(tmp_path)
    reader = ms.MobileSshDeviceStore(tmp_path)
    writer.enroll("phone", _public_key(), launcher=sys.executable)
    assert reader.list_devices()[0]["active"] is True
    writer.revoke("phone")
    listed = reader.list_devices()[0]
    assert listed["active"] is False
    assert listed["revoked_at"]


def test_registry_removed_underneath_a_live_store_fails_closed(tmp_path) -> None:
    writer = ms.MobileSshDeviceStore(tmp_path)
    reader = ms.MobileSshDeviceStore(tmp_path)
    enrolled = writer.enroll("phone", _public_key(), launcher=sys.executable)
    binding = _binding(enrolled.authorized_keys_line)
    token = str(reader.mint("phone", binding)["token"])
    claims = json.loads(ta._b64url_decode(token.split(".", 1)[0]))
    (tmp_path / ms.MOBILE_SSH_DIR / ms.MOBILE_SSH_STORE).unlink()
    reader.refresh()
    assert reader.validate_claims(claims) == (False, "mobile device not enrolled")


def test_validate_claims_never_touches_the_filesystem(mobile_store, monkeypatch) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    token = str(mobile_store.mint("phone", _binding(enrolled.authorized_keys_line))["token"])
    claims = json.loads(ta._b64url_decode(token.split(".", 1)[0]))

    def _boom(*_args, **_kwargs):
        raise AssertionError("validate_claims must not touch the registry")

    monkeypatch.setattr(mobile_store, "_refresh_if_changed_locked", _boom)
    monkeypatch.setattr(mobile_store, "_load", _boom)
    assert mobile_store.validate_claims(claims) == (True, "")
    assert ms.validate_mobile_token_claims(claims) == (True, "")


def test_claims_fail_closed_before_the_registry_is_constructed(monkeypatch) -> None:
    monkeypatch.setattr(ms, "_store_singleton", None)
    assert ms.validate_mobile_token_claims({"kind": ms.MOBILE_TOKEN_KIND}) == (
        False,
        "mobile device registry unavailable",
    )


def test_refresh_swallows_a_broken_registry(monkeypatch) -> None:
    def _broken():
        raise ms.MobileSshError("store_unavailable", "broken", status=503)

    monkeypatch.setattr(ms, "get_mobile_ssh_store", _broken)
    ms.refresh_mobile_ssh_store()


def test_failed_enrollment_write_is_not_honored_in_memory(mobile_store, monkeypatch) -> None:
    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ms, "atomic_write", fail_write)
    with pytest.raises(ms.MobileSshError) as exc:
        mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    assert exc.value.code == "store_unavailable"
    assert exc.value.status == 503
    assert mobile_store.list_devices() == []


def test_failed_revocation_write_keeps_memory_matching_the_durable_registry(
    mobile_store, monkeypatch, tmp_path
) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    token = str(mobile_store.mint("phone", _binding(enrolled.authorized_keys_line))["token"])
    claims = json.loads(ta._b64url_decode(token.split(".", 1)[0]))

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ms, "atomic_write", fail_write)
    with pytest.raises(ms.MobileSshError) as failed:
        mobile_store.revoke("phone")
    assert failed.value.code == "store_unavailable"
    assert mobile_store.list_devices()[0]["active"] is True
    assert ms.MobileSshDeviceStore(tmp_path).list_devices()[0]["active"] is True
    assert ms.validate_mobile_token_claims(claims) == (True, "")


def test_successful_revocation_invalidates_outstanding_tokens(mobile_store) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    token = str(mobile_store.mint("phone", _binding(enrolled.authorized_keys_line))["token"])
    claims = json.loads(ta._b64url_decode(token.split(".", 1)[0]))
    mobile_store.revoke("phone")
    assert ms.validate_mobile_token_claims(claims) == (False, "mobile device revoked")


def test_enrollment_count_is_bounded(tmp_path) -> None:
    store = ms.MobileSshDeviceStore(tmp_path)
    for index in range(ms.MOBILE_SSH_MAX_DEVICES):
        store.enroll(f"phone-{index}", _public_key(index + 1), launcher=sys.executable)
    with pytest.raises(ms.MobileSshError) as limited:
        store.enroll(
            "one-more", _public_key(ms.MOBILE_SSH_MAX_DEVICES + 1), launcher=sys.executable
        )
    assert limited.value.code == "device_limit_reached"
    store.revoke("phone-0")
    store.enroll("phone-0", _public_key(ms.MOBILE_SSH_MAX_DEVICES + 1), launcher=sys.executable)


def test_over_cap_registry_fails_closed() -> None:
    record = {"version": 1, "devices": [{}] * (ms.MOBILE_SSH_MAX_DEVICES + 1)}
    with pytest.raises(ValueError):
        ms.MobileSshDeviceStore._parse_registry(record)


def test_refresh_failure_empties_the_snapshot(mobile_store, tmp_path) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    token = str(mobile_store.mint("phone", _binding(enrolled.authorized_keys_line))["token"])
    claims = json.loads(ta._b64url_decode(token.split(".", 1)[0]))
    (tmp_path / ms.MOBILE_SSH_DIR / ms.MOBILE_SSH_STORE).write_text("{not json")
    ms.refresh_mobile_ssh_store()
    assert ms.validate_mobile_token_claims(claims) == (False, "mobile device not enrolled")


def test_refresh_translates_a_directory_os_error(tmp_path, monkeypatch) -> None:
    store = ms.MobileSshDeviceStore(tmp_path)

    def fail_mkdir(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with pytest.raises(ms.MobileSshError) as exc:
        store._ensure_dir()
    assert exc.value.code == "store_unavailable"


def test_device_record_carries_pinned_port_and_home(tmp_path) -> None:
    store = ms.MobileSshDeviceStore(tmp_path)
    store.enroll("phone", _public_key(), launcher=sys.executable, gateway_port=6100)
    device = ms.MobileSshDeviceStore(tmp_path).list_devices()[0]
    assert device["gateway_port"] == 6100
    assert device["home"] == str(tmp_path.resolve())


def test_records_without_pinned_port_and_home_still_load(tmp_path) -> None:
    _write_registry(tmp_path, {"version": 1, "devices": [_record()]})
    device = ms.MobileSshDeviceStore(tmp_path).list_devices()[0]
    assert device["gateway_port"] is None
    assert device["home"] is None


def test_list_flags_port_and_home_drift() -> None:
    devices: list[dict[str, object]] = [
        {"gateway_port": 5476, "home": "/h"},
        {"gateway_port": 6100, "home": "/old"},
        {"gateway_port": None, "home": None},
    ]
    mobile_handlers._mark_drift(devices, 5476, "/h")
    assert [d["drift"] for d in devices] == [[], ["gateway_port", "home"], []]


def test_public_key_file_refuses_a_protected_path(monkeypatch, tmp_path) -> None:
    key = tmp_path / "id_ed25519.pub"
    key.write_text(_public_key())

    import kiro_crew.hooks as hooks

    monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", lambda *_a, **_k: None)
    with pytest.raises(mobile_cli._GatewayCallError) as exc:
        mobile_cli._read_public_key(str(key))
    assert exc.value.code == "public_key_path_refused"


def test_public_key_file_read_is_bounded(tmp_path) -> None:
    key = tmp_path / "big.pub"
    key.write_text("a" * (mobile_cli._MAX_PUBLIC_KEY_BYTES + 1))
    with pytest.raises(mobile_cli._GatewayCallError) as exc:
        mobile_cli._read_public_key(str(key))
    assert exc.value.code == "public_key_too_large"


def test_public_key_file_must_be_a_regular_file(tmp_path) -> None:
    with pytest.raises(mobile_cli._GatewayCallError) as exc:
        mobile_cli._read_public_key(str(tmp_path))
    assert exc.value.code == "public_key_unreadable"


def test_public_key_file_rejects_a_private_key(tmp_path) -> None:
    key = tmp_path / "phone_key"
    key.write_text("OPENSSH PRIVATE KEY\n")
    with pytest.raises(mobile_cli._GatewayCallError) as exc:
        mobile_cli._read_public_key(str(key))
    assert exc.value.code == "public_key_is_private"


def test_public_key_file_reads_a_public_key(tmp_path) -> None:
    key = tmp_path / "phone.pub"
    key.write_text(_public_key())
    assert mobile_cli._read_public_key(str(key)) == _public_key()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_lifetime_claims_are_rejected(mobile_store, bad: float) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    claims = {
        "kind": ms.MOBILE_TOKEN_KIND,
        "aud": ms.MOBILE_TOKEN_AUDIENCE,
        "scope": ms.MOBILE_TOKEN_SCOPE,
        "device_id": "phone",
        "key_sha256": enrolled.device.key_sha256,
        "enrollment_id": enrolled.device.enrollment_id,
        "no_refresh": "1",
        "sub": "mobile:phone",
        "iat": 1000.0,
        "session_exp": bad,
    }
    assert mobile_store.validate_claims(claims) == (False, "mobile token lifetime invalid")
    claims["iat"], claims["session_exp"] = bad, 1900.0
    assert mobile_store.validate_claims(claims) == (False, "mobile token lifetime invalid")


def test_mobile_token_claims_ttl_and_no_secret_persistence(mobile_store, tmp_path, caplog) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    binding = _binding(enrolled.authorized_keys_line)
    document = mobile_store.mint("phone", binding)
    token = str(document["token"])
    claims = json.loads(ta._b64url_decode(token.split(".", 1)[0]))
    assert document["expires_in"] == ms.MOBILE_TOKEN_TTL_SECS
    assert claims["session_exp"] - claims["iat"] == ms.MOBILE_TOKEN_TTL_SECS
    assert claims["kind"] == ms.MOBILE_TOKEN_KIND
    assert claims["aud"] == ms.MOBILE_TOKEN_AUDIENCE
    assert claims["scope"] == ms.MOBILE_TOKEN_SCOPE
    assert claims["no_refresh"] == "1"
    persisted = (tmp_path / ms.MOBILE_SSH_DIR / ms.MOBILE_SSH_STORE).read_text()
    assert token not in persisted
    assert token not in caplog.text


@pytest.mark.parametrize(
    ("claim", "value"),
    [("aud", "wrong-audience"), ("scope", "gateway:admin"), ("no_refresh", "0")],
)
def test_mobile_token_wrong_audience_or_scope_is_rejected(
    mobile_store, claim: str, value: str
) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    claims = {
        "kind": ms.MOBILE_TOKEN_KIND,
        "aud": ms.MOBILE_TOKEN_AUDIENCE,
        "scope": ms.MOBILE_TOKEN_SCOPE,
        "device_id": enrolled.device.device_id,
        "key_sha256": enrolled.device.key_sha256,
        "enrollment_id": enrolled.device.enrollment_id,
        "no_refresh": "1",
    }
    claims[claim] = value
    token = ta.generate_token("mobile:phone", ms.MOBILE_TOKEN_TTL_SECS, extra=claims)
    valid, _user, reason = ta.validate_token(token)
    assert valid is False
    assert reason == "mobile token audience or scope invalid"


def test_mobile_token_expiration_is_enforced(mobile_store, monkeypatch) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    binding = _binding(enrolled.authorized_keys_line)
    monkeypatch.setattr(ta.time, "time", lambda: 1000.0)
    token = str(mobile_store.mint("phone", binding)["token"])
    monkeypatch.setattr(ta.time, "time", lambda: 1901.0)
    valid, _user, reason = ta.validate_token(token)
    assert valid is False
    assert reason == "token expired"


@pytest.mark.parametrize(
    "path",
    [
        "/api/status",
        "/api/sessions",
        "/api/chat/slot",
        "/api/tasks/123",
        "/api/spawn",
        "/api/spawn/run-1",
        "/api/suggestions",
        "/api/optimizer/optimize",
        "/api/upload/file",
        "/api/artifact-folders",
        "/api/cron-folders",
        "/api/ask-question/pending",
        "/api/ask-question/dismiss",
        "/api/ask-question/ask-1/answer",
        "/api/outbox/report.pdf",
    ],
)
def test_mobile_scope_allows_native_gateway_paths(path: str) -> None:
    assert ms.mobile_token_path_allowed(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/security/posture",
        "/api/config/kirocrew",
        "/api/shutdown",
        "/v1/chat",
        "/api/ask-question",
        "/api/outbox",
        "/api/outbox/notify",
        "/api/ws",
        "/api/file-raw",
    ],
)
def test_mobile_scope_denies_admin_and_page_paths(path: str) -> None:
    assert ms.mobile_token_path_allowed(path) is False


@pytest.mark.parametrize("path", ["/api/artifact-folders", "/api/cron-folders"])
def test_mobile_scope_allows_only_reads_of_folder_collections(path: str) -> None:
    assert ms.mobile_token_path_allowed(path, "GET") is True
    assert ms.mobile_token_path_allowed(path, "head") is True
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert ms.mobile_token_path_allowed(path, method) is False


def _gateway_paths() -> set[str]:
    app = web.Application()
    _register_mcp_routes(app)
    register_all(app)
    return {
        str(route.resource.canonical) for route in app.router.routes() if route.resource is not None
    }


def test_mobile_allowlist_names_only_registered_routes() -> None:
    paths = _gateway_paths()
    for exact in ms.MOBILE_TOKEN_ALLOWED_PATHS | ms.MOBILE_TOKEN_ALLOWED_COLLECTIONS:
        assert exact in paths, exact
    for prefix in ms.MOBILE_TOKEN_ALLOWED_PREFIXES:
        assert any(p.startswith(prefix) for p in paths), prefix
    for denied in ms.MOBILE_TOKEN_DENIED_PATHS:
        assert denied in paths, denied


def test_ssh_device_is_a_governed_mobile_connect_method() -> None:
    ids = [m.id for m in DefaultMobileConnectProvider().connect_methods()]
    assert "ssh_device" in ids


def _set_body(
    request: MagicMock,
    payload: object,
    *,
    content_length: int | None = None,
    chunk_size: int | None = None,
) -> None:
    raw = json.dumps(payload).encode()
    request.content_length = len(raw) if content_length is None else content_length
    request.content_type = "application/json"
    request.charset = None
    request.can_read_body = True
    request.content = MagicMock()
    request.content.read = AsyncMock(side_effect=_stream_reader(raw, chunk_size))
    request.content.iter_chunked = lambda limit: _chunk_iter(raw, limit, chunk_size)


async def _chunk_iter(raw: bytes, limit: int, chunk_size: int | None):
    step = limit if chunk_size is None else min(limit, chunk_size)
    for start in range(0, len(raw), step):
        yield raw[start : start + step]


def _stream_reader(raw: bytes, chunk_size: int | None):
    """Model aiohttp's StreamReader.read(n): at most n bytes per call, b"" at EOF."""
    state = {"offset": 0}

    async def read(limit: int) -> bytes:
        start = state["offset"]
        step = limit if chunk_size is None else min(limit, chunk_size)
        chunk = raw[start : start + step]
        state["offset"] = start + len(chunk)
        return chunk

    return read


def _token_request(payload: object, **body_kwargs) -> MagicMock:
    request = _request(
        path="/api/mobile/ssh/token", method="POST", headers={"X-Local-Secret": "secret"}
    )
    request.app = {"local_secret": "secret"}
    _set_body(request, payload, **body_kwargs)
    return request


@pytest.mark.asyncio
async def test_api_body_is_bounded_without_content_length(monkeypatch) -> None:
    store = MagicMock()
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())
    request = _token_request({"pad": "x" * 8192}, content_length=None)
    request.content_length = None
    response = await mobile_handlers.api_mobile_ssh_token(request)
    assert response.status == 413
    assert json.loads(response.text)["code"] == "payload_too_large"
    assert response.headers["Cache-Control"] == "no-store"
    store.mint.assert_not_called()


@pytest.mark.asyncio
async def test_api_body_reads_every_chunk_of_a_chunked_body(monkeypatch) -> None:
    store = MagicMock()
    store.mint.return_value = {"ok": True}
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())
    request = _token_request(
        {"device_id": "phone", "binding": "f" * 64}, content_length=None, chunk_size=7
    )
    request.content_length = None
    response = await mobile_handlers.api_mobile_ssh_token(request)
    assert response.status == 200
    assert store.mint.call_args.args == ("phone", "f" * 64)


@pytest.mark.asyncio
async def test_api_body_over_cap_is_rejected_even_when_chunked(monkeypatch) -> None:
    store = MagicMock()
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())
    request = _token_request({"pad": "x" * 8192}, content_length=None, chunk_size=64)
    request.content_length = None
    response = await mobile_handlers.api_mobile_ssh_token(request)
    assert response.status == 413
    assert json.loads(response.text)["code"] == "payload_too_large"
    store.mint.assert_not_called()


@pytest.mark.asyncio
async def test_api_requires_a_host_owned_local_caller(monkeypatch) -> None:
    store = MagicMock()
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())
    monkeypatch.setattr(mobile_handlers, "local_owner_bootstrap_allowed", lambda _r: False)
    request = _token_request({"device_id": "phone", "binding": "f" * 64})
    response = await mobile_handlers.api_mobile_ssh_token(request)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_owner_token_refused"
    store.mint.assert_not_called()


@pytest.mark.asyncio
async def test_api_mint_and_enroll_honor_the_mobile_connect_seam(monkeypatch) -> None:
    store = MagicMock()
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())
    monkeypatch.setattr(mobile_handlers, "mint_denied_reason", lambda method: f"{method} off")
    response = await mobile_handlers.api_mobile_ssh_token(
        _token_request({"device_id": "phone", "binding": "f" * 64})
    )
    assert response.status == 403
    assert json.loads(response.text) == {
        "schema": ms.MOBILE_ERROR_SCHEMA,
        "ok": False,
        "code": "mobile_connect_denied",
        "error": "ssh_device off",
    }
    enroll = _request(
        path="/api/mobile/ssh/enroll", method="POST", headers={"X-Local-Secret": "secret"}
    )
    enroll.app = {"local_secret": "secret", "port": 5476}
    _set_body(enroll, {"device_id": "phone", "ssh_host": "crew.example", "public_key": "k"})
    response = await mobile_handlers.api_mobile_ssh_enroll(enroll)
    assert response.status == 403
    store.enroll.assert_not_called()
    store.mint.assert_not_called()


def test_launcher_falls_back_to_the_installed_launcher_under_python_m(
    monkeypatch, tmp_path
) -> None:
    module_main = tmp_path / "kiro_crew" / "__main__.py"
    module_main.parent.mkdir()
    module_main.write_text("")
    module_main.chmod(0o755)
    launcher = tmp_path / "bin" / "kirocrew"
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(module_main)])
    monkeypatch.setattr(shutil, "which", lambda name: str(launcher) if name == "kirocrew" else None)
    assert mobile_handlers._launcher() == str(launcher)


def test_launcher_prefers_unresolved_path_shim(monkeypatch, tmp_path) -> None:
    target = tmp_path / "versions" / "1.0" / "kirocrew"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    shim = tmp_path / "bin" / "kirocrew"
    shim.parent.mkdir()
    shim.symlink_to(target)
    monkeypatch.setattr(mobile_handlers.shutil, "which", lambda name: str(shim))
    monkeypatch.setattr(mobile_handlers.sys, "argv", [str(target)])
    assert mobile_handlers._launcher() == str(shim)


def test_launcher_ignores_an_unrelated_path_kirocrew(monkeypatch, tmp_path) -> None:
    running = tmp_path / "current" / "kirocrew"
    other = tmp_path / "other" / "kirocrew"
    for launcher in (running, other):
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
    monkeypatch.setattr(mobile_handlers.shutil, "which", lambda name: str(other))
    monkeypatch.setattr(mobile_handlers.sys, "argv", [str(running)])
    assert mobile_handlers._launcher() == str(running)


def test_launcher_fails_closed_without_a_runnable_launcher(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mobile_handlers.shutil, "which", lambda name: None)
    monkeypatch.setattr(mobile_handlers.sys, "argv", [str(tmp_path / "missing")])
    with pytest.raises(ms.MobileSshError) as exc:
        mobile_handlers._launcher()
    assert exc.value.code == "launcher_unavailable"


def _request(
    *,
    path: str,
    query: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    remote: str = "127.0.0.1",
    method: str = "GET",
) -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.path = path
    request.query = query or {}
    request.cookies = cookies or {}
    request.headers = headers or {"Host": "localhost:5476"}
    request.remote = remote
    request.method = method
    return request


async def _ok_handler(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@pytest.mark.asyncio
async def test_link_exchange_carries_mobile_claims_and_issues_no_refresh(mobile_store) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    token = str(mobile_store.mint("phone", _binding(enrolled.authorized_keys_line))["token"])
    response = await ta.token_auth_middleware()(
        _request(path="/api/status", query={"token": token}), _ok_handler
    )
    assert response.status == 200
    cookie = response.cookies["mc_token_5476"].value
    assert cookie != token
    claims = json.loads(ta._b64url_decode(cookie.split(".", 1)[0]))
    for name in ("kind", "aud", "scope", "device_id", "key_sha256", "enrollment_id"):
        original = json.loads(ta._b64url_decode(token.split(".", 1)[0]))
        assert claims[name] == original[name]
    refresh = response.cookies[refresh_cookie_name("5476")]
    assert refresh["max-age"] == "0"


@pytest.mark.asyncio
async def test_mobile_scope_is_enforced_by_middleware(mobile_store) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    token = str(mobile_store.mint("phone", _binding(enrolled.authorized_keys_line))["token"])
    response = await ta.token_auth_middleware()(
        _request(path="/api/security/posture", query={"token": token}), _ok_handler
    )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "mobile_scope_denied"


@pytest.mark.asyncio
async def test_mobile_token_is_never_the_dashboard_owner(mobile_store, monkeypatch) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    token = str(mobile_store.mint("phone", _binding(enrolled.authorized_keys_line))["token"])
    request = _request(path="/api/status", query={"token": token})
    response = await ta.token_auth_middleware()(request, _ok_handler)
    assert response.status == 200
    stamped = {c.args[0]: c.args[1] for c in request.__setitem__.call_args_list}
    assert stamped["is_dashboard_user"] is False
    assert ta._is_dashboard_user("", ta.generate_token("person", ttl_seconds=60)) is True
    assert ta._is_dashboard_user("", token) is False


@pytest.mark.asyncio
async def test_ws_is_denied_to_mobile_tokens_by_middleware(mobile_store) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    token = str(mobile_store.mint("phone", _binding(enrolled.authorized_keys_line))["token"])
    response = await ta.token_auth_middleware()(
        _request(path="/api/ws", query={"token": token}), _ok_handler
    )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "mobile_scope_denied"


@pytest.mark.asyncio
async def test_middleware_refreshes_the_registry_off_loop_for_mobile_tokens_only(
    mobile_store, monkeypatch
) -> None:
    import threading

    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    token = str(mobile_store.mint("phone", _binding(enrolled.authorized_keys_line))["token"])
    threads: list[threading.Thread] = []
    monkeypatch.setattr(
        ms, "refresh_mobile_ssh_store", lambda: threads.append(threading.current_thread())
    )
    await ta.token_auth_middleware()(
        _request(path="/api/status", query={"token": token}), _ok_handler
    )
    assert len(threads) == 1
    assert threads[0] is not threading.main_thread()
    legacy = ta.generate_token("legacy-user", ttl_seconds=60)
    await ta.token_auth_middleware()(
        _request(path="/api/status", query={"token": legacy}), _ok_handler
    )
    assert len(threads) == 1


@pytest.mark.asyncio
async def test_warm_auth_singletons_never_loads_the_mobile_registry(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ms, "refresh_mobile_ssh_store", lambda: calls.append("refresh"))
    monkeypatch.setattr(ms, "get_mobile_ssh_store", lambda: calls.append("get"))
    await ta.warm_auth_singletons()
    assert calls == []


@pytest.mark.asyncio
async def test_legacy_gateway_token_behavior_is_unchanged() -> None:
    token = ta.generate_token("legacy-user", ttl_seconds=60)
    assert ta.validate_token(token) == (True, "legacy-user", "")
    response = await ta.token_auth_middleware()(
        _request(path="/api/status", query={"token": token}), _ok_handler
    )
    assert response.status == 200


def test_forced_command_requires_openssh_context(monkeypatch) -> None:
    for name in ("SSH_ORIGINAL_COMMAND", "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    assert mobile_cli._forced_command_context_valid() is False
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", ms.MOBILE_ORIGINAL_COMMAND)
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.5 50000 192.0.2.10 22")
    assert mobile_cli._forced_command_context_valid() is True
    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    assert mobile_cli._forced_command_context_valid() is False


def test_cli_token_stdout_is_one_json_object_and_stderr_is_empty(monkeypatch, capsys) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", ms.MOBILE_ORIGINAL_COMMAND)
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.5 50000 192.0.2.10 22")
    monkeypatch.delenv("SSH_TTY", raising=False)
    expected = {"schema": ms.MOBILE_TOKEN_SCHEMA, "token": "test-token"}
    monkeypatch.setattr(mobile_cli, "_call_gateway", lambda *_a, **_kw: expected)
    rc = mobile_cli.run_mobile_ssh(
        argparse.Namespace(
            mobile_ssh_action="token", port=5476, device_id="phone", binding="a" * 64
        )
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out) == expected


def test_cli_token_reads_the_secret_from_the_enrolled_home(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", ms.MOBILE_ORIGINAL_COMMAND)
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.5 50000 192.0.2.10 22")
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    seen = []
    monkeypatch.setattr(
        mobile_cli,
        "_call_gateway",
        lambda *_a, **_kw: seen.append(os.environ.get("KIROCREW_HOME")) or {"ok": True},
    )
    args = argparse.Namespace(
        mobile_ssh_action="token",
        port=5492,
        device_id="phone",
        binding="a" * 64,
        home=str(tmp_path),
    )
    assert mobile_cli.run_mobile_ssh(args) == 0
    assert seen == [str(tmp_path)]
    args.home = str(tmp_path / "missing")
    assert mobile_cli.run_mobile_ssh(args) != 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["code"] == "invalid_home"


def test_cli_without_an_action_prints_the_json_error(capsys) -> None:
    assert mobile_cli.run_mobile_ssh(argparse.Namespace(mobile_ssh_action=None)) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "missing_action"


def test_cli_never_sends_the_secret_to_an_unverified_listener(monkeypatch) -> None:
    monkeypatch.setattr(mobile_cli, "port_is_gateway_owned", lambda _port: False)
    monkeypatch.setattr(
        mobile_cli, "read_local_secret", lambda *_a, **_kw: pytest.fail("secret was read")
    )
    with pytest.raises(mobile_cli._GatewayCallError) as exc:
        mobile_cli._call_gateway(5492, "/api/mobile/ssh/devices", method="GET")
    assert exc.value.code == "gateway_unverified"


def test_forced_command_home_is_pinned_before_cli_startup(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    args = argparse.Namespace(mobile_ssh_action="token", home=str(tmp_path))
    assert mobile_cli.pin_enrolled_home(args) == 0
    assert os.environ["KIROCREW_HOME"] == str(tmp_path)
    monkeypatch.delenv("KIROCREW_HOME")
    assert mobile_cli.pin_enrolled_home(argparse.Namespace(mobile_ssh_action="list")) == 0
    assert "KIROCREW_HOME" not in os.environ


def test_directory_fsync_after_the_rename_is_best_effort(mobile_store, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(ms, "fsync_dir", lambda path, **kw: calls.append(kw))
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    assert calls and all(kw.get("best_effort") is True for kw in calls)


def test_cli_management_commands_default_to_running_gateway_port(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(mobile_cli, "resolve_client_port", lambda port: port or 5477)
    monkeypatch.setattr(
        mobile_cli,
        "_call_gateway",
        lambda port, path, **_kw: calls.append((port, path)) or {"ok": True},
    )
    assert mobile_cli.run_mobile_ssh(argparse.Namespace(mobile_ssh_action="list", port=None)) == 0
    assert mobile_cli.run_mobile_ssh(argparse.Namespace(mobile_ssh_action="list", port=6000)) == 0
    capsys.readouterr()
    assert calls == [(5477, "/api/mobile/ssh/devices"), (6000, "/api/mobile/ssh/devices")]


_ENROLLMENT = {
    "schema": ms.MOBILE_ENROLLMENT_SCHEMA,
    "ok": True,
    "authorized_keys_line": "restricted-key-line",
    "ssh_host": "crewbox.local",
    "ssh_port": 22,
    "ssh_username": "dev",
    "host_key_fingerprint": "SHA256:" + "A" * 43,
    "gateway_port": 5492,
}


def test_pairing_uri_carries_only_the_ssh_endpoint() -> None:
    uri = mobile_cli.pairing_uri(_ENROLLMENT)
    assert uri.startswith("kiro-crew://c/")
    blob = uri.removeprefix("kiro-crew://c/")
    decoded = json.loads(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))
    assert decoded == {
        "version": 1,
        "host": "crewbox.local",
        "sshPort": 22,
        "username": "dev",
        "remoteGatewayPort": 5492,
        "hostKeyFingerprint": "SHA256:" + "A" * 43,
    }
    assert "restricted-key-line" not in uri


@pytest.mark.parametrize("qr", [False, True])
def test_cli_enroll_qr_goes_to_stderr_and_keeps_stdout_json(monkeypatch, capsys, qr) -> None:
    monkeypatch.setattr(mobile_cli, "resolve_client_port", lambda port: 5492)
    monkeypatch.setattr(mobile_cli, "_read_public_key", lambda _path: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(mobile_cli, "_call_gateway", lambda *_a, **_kw: dict(_ENROLLMENT))
    args = argparse.Namespace(
        mobile_ssh_action="enroll",
        port=None,
        device_id="phone",
        label="",
        ssh_host="crewbox.local",
        public_key_file="-",
        qr=qr,
    )
    assert mobile_cli.run_mobile_ssh(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == _ENROLLMENT
    assert ("Scan in Kiro Mobile" in captured.err) is qr


def test_cli_error_stdout_is_stable_json_and_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    rc = mobile_cli.run_mobile_ssh(
        argparse.Namespace(
            mobile_ssh_action="token", port=5476, device_id="phone", binding="a" * 64
        )
    )
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert rc != 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert document == {
        "schema": ms.MOBILE_ERROR_SCHEMA,
        "ok": False,
        "code": "forced_command_required",
        "error": "token minting requires the enrolled OpenSSH forced command",
    }


@pytest.mark.asyncio
async def test_api_requires_loopback_local_secret(monkeypatch) -> None:
    store = MagicMock()
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())
    request = _request(
        path="/api/mobile/ssh/devices",
        headers={"X-Local-Secret": "wrong"},
    )
    request.app = {"local_secret": "expected"}
    response = await mobile_handlers.api_mobile_ssh_devices(request)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "local_auth_failed"
    store.list_devices.assert_not_called()


@pytest.mark.asyncio
async def test_non_ascii_local_secret_is_refused_not_crashed(monkeypatch) -> None:
    store = MagicMock()
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())
    request = _request(
        path="/api/mobile/ssh/devices",
        headers={"X-Local-Secret": "wr\u00f6ng"},
    )
    request.app = {"local_secret": "expected"}
    response = await mobile_handlers.api_mobile_ssh_devices(request)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "local_auth_failed"
    store.list_devices.assert_not_called()


@pytest.mark.asyncio
async def test_api_enrollment_returns_generic_host_username_and_pin(monkeypatch) -> None:
    store = MagicMock()
    store.enroll.return_value = SimpleNamespace(
        device=SimpleNamespace(public_metadata=lambda: {"device_id": "phone", "active": True}),
        authorized_keys_line="restricted-key-line",
    )
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())
    monkeypatch.setattr(mobile_handlers, "_launcher", lambda: sys.executable)
    monkeypatch.setattr(
        mobile_handlers,
        "discover_host_ssh_identity",
        lambda: ms.HostSshIdentity("crewuser", "ssh-ed25519", "SHA256:host-pin"),
    )
    request = _request(
        path="/api/mobile/ssh/enroll",
        method="POST",
        headers={"X-Local-Secret": "secret"},
    )
    request.app = {"local_secret": "secret", "port": 0}
    request.transport.get_extra_info.return_value = ("127.0.0.1", 5477)
    _set_body(
        request,
        {
            "device_id": "phone",
            "label": "Phone",
            "ssh_host": "crewbox.local",
            "public_key": _public_key(),
        },
    )
    response = await mobile_handlers.api_mobile_ssh_enroll(request)
    body = json.loads(response.text)
    assert response.status == 200
    assert body["ssh_host"] == "crewbox.local"
    assert body["ssh_username"] == "crewuser"
    assert body["host_key_fingerprint"] == "SHA256:host-pin"
    assert body["authorized_keys_path"] == "~/.ssh/authorized_keys"
    assert body["gateway_host"] == "127.0.0.1"
    assert body["gateway_port"] == 5477
    assert store.enroll.call_args.kwargs["gateway_port"] == 5477


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("key_sha256", "0" * 64, "mobile device key mismatch"),
        ("enrollment_id", "0" * 32, "mobile device enrollment replaced"),
        ("sub", "mobile:another", "mobile token subject invalid"),
    ],
)
def test_signed_mobile_token_binding_claim_mismatches_are_rejected(
    mobile_store, field: str, value: str, reason: str
) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    claims = {
        "kind": ms.MOBILE_TOKEN_KIND,
        "aud": ms.MOBILE_TOKEN_AUDIENCE,
        "scope": ms.MOBILE_TOKEN_SCOPE,
        "device_id": enrolled.device.device_id,
        "key_sha256": enrolled.device.key_sha256,
        "enrollment_id": enrolled.device.enrollment_id,
        "no_refresh": "1",
    }
    subject = "mobile:phone"
    if field == "sub":
        subject = value
    else:
        claims[field] = value
    token = ta.generate_token(subject, ms.MOBILE_TOKEN_TTL_SECS, extra=claims)
    valid, _user, actual_reason = ta.validate_token(token)
    assert valid is False
    assert actual_reason == reason


@pytest.mark.asyncio
async def test_api_token_mint_uses_local_secret_without_logging_or_persisting_it(
    mobile_store, monkeypatch, tmp_path
) -> None:
    enrolled = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    binding = _binding(enrolled.authorized_keys_line)
    audit = MagicMock()
    monkeypatch.setattr(mobile_handlers, "sel", lambda: audit)
    request = _request(
        path="/api/mobile/ssh/token",
        method="POST",
        headers={"X-Local-Secret": "local-test-secret"},
    )
    request.app = {"local_secret": "local-test-secret"}
    _set_body(request, {"device_id": "phone", "binding": binding})
    response = await mobile_handlers.api_mobile_ssh_token(request)
    body = json.loads(response.text)
    token = body["token"]
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert token not in repr(audit.mock_calls)
    persisted = (tmp_path / ms.MOBILE_SSH_DIR / ms.MOBILE_SSH_STORE).read_text()
    assert token not in persisted
    assert "local-test-secret" not in persisted


@pytest.mark.asyncio
async def test_api_refuses_non_loopback_even_with_local_secret(monkeypatch) -> None:
    store = MagicMock()
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())
    request = _request(
        path="/api/mobile/ssh/devices",
        headers={"X-Local-Secret": "secret"},
        remote="192.0.2.10",
    )
    request.app = {"local_secret": "secret"}
    response = await mobile_handlers.api_mobile_ssh_devices(request)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "local_only"
    store.list_devices.assert_not_called()


def test_mobile_registry_is_on_sensitive_path_floor() -> None:
    from kiro_crew.security import is_sensitive_path

    assert is_sensitive_path("~/.kiro/crew/mobile-ssh/devices.json") is True
    assert is_sensitive_path("~/.kiro/crew/mobile-ssh/devices.lock") is True
    assert is_sensitive_path("~/.kirocrew/mobile-ssh/devices.json") is True
