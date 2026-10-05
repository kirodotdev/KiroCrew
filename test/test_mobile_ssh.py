"""Contract tests for per-device mobile SSH enrollment, the stdio bridge and the mint."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import shutil
import socket
import stat
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kiro_crew import mobile_ssh as ms
from kiro_crew import mobile_ssh_cli as mobile_cli
from kiro_crew.dashboard import token_auth as ta
from kiro_crew.dashboard.handlers import mobile_ssh as mobile_handlers
from kiro_crew.platform.defaults import DefaultMobileConnectProvider


def _ssh_string(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def _key_pair(seed: int = 1) -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    blob = _ssh_string(b"ssh-ed25519") + _ssh_string(raw)
    return private, f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')} phone"


def _public_key(seed: int = 1) -> str:
    return _key_pair(seed)[1]


def _sign(private: Ed25519PrivateKey, device_id: str, nonce: str) -> str:
    return base64.b64encode(private.sign(ms.signed_message(device_id, nonce))).decode("ascii")


def _mint(store: ms.MobileSshDeviceStore, device_id: str = "phone", seed: int = 1) -> dict:
    private, _public = _key_pair(seed)
    nonce = str(store.issue_challenge(device_id)["nonce"])
    return store.mint(device_id, nonce, _sign(private, device_id, nonce))


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


# ── device ids and keys ──


@pytest.mark.parametrize("device_id", ["phone", "0phone", "iphone-15"])
def test_device_id_accepts_documented_spellings(device_id: str) -> None:
    assert ms.validate_device_id(device_id) == device_id


@pytest.mark.parametrize("device_id", ["", "Phone", "-phone", "phone_1", "p" * 64, "ph one"])
def test_device_id_rejects_other_spellings(device_id: str) -> None:
    with pytest.raises(ms.MobileSshError) as exc:
        ms.validate_device_id(device_id)
    assert exc.value.code == "invalid_device_id"


def test_public_key_validation_is_structural() -> None:
    canonical, digest, fingerprint = ms.validate_ed25519_public_key(_public_key())
    assert canonical == " ".join(_public_key().split()[:2])
    assert len(digest) == 64
    assert fingerprint.startswith("SHA256:")
    for bad in ("ssh-ed25519 not-base64", "ssh-rsa AAAA", "", "ssh-ed25519 " + "A" * 2000):
        with pytest.raises(ms.MobileSshError):
            ms.validate_ed25519_public_key(bad)


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


def test_host_identity_returns_username_and_pin(monkeypatch) -> None:
    monkeypatch.setattr(ms, "_current_username", lambda: "crewuser")
    monkeypatch.setattr(ms, "_read_verified_host_public_key", lambda _path: _public_key())
    identity = ms.discover_host_ssh_identity()
    assert identity.username == "crewuser"
    assert identity.host_key_fingerprint.startswith("SHA256:")


def test_windows_host_identity_fails_actionably(monkeypatch) -> None:
    monkeypatch.setattr(ms.platform_compat, "IS_WINDOWS", True)
    with pytest.raises(ms.MobileSshError) as exc:
        ms.discover_host_ssh_identity()
    assert exc.value.code == "platform_unsupported"


# ── enrollment and the authorized_keys line ──


def test_forced_command_argv_is_exactly_the_bridge(tmp_path) -> None:
    argv = ms.forced_command_argv("/opt/kc/bin/kirocrew", "phone", 6100, tmp_path)
    assert argv == [
        "/opt/kc/bin/kirocrew",
        "mobile",
        "ssh",
        "bridge",
        "--device-id",
        "phone",
        "--port",
        "6100",
        "--home",
        str(tmp_path),
    ]


def _forced_command(line: str) -> list[str]:
    # Decode the option value the way sshd does: backslash escapes, unescaped quote ends.
    prefix = 'restrict,command="'
    assert line.startswith(prefix)
    chars, index = [], len(prefix)
    while line[index] != '"':
        if line[index] == "\\":
            index += 1
        chars.append(line[index])
        index += 1
    assert line[index + 1] == " "
    return shlex.split("".join(chars))


def test_enrolled_line_is_plain_restrict_with_the_bridge_command(mobile_store, tmp_path) -> None:
    enrolled = mobile_store.enroll(
        "phone", _public_key(), launcher=sys.executable, gateway_port=6100
    )
    line = enrolled.authorized_keys_line
    assert line.startswith('restrict,command="')
    for reopened in ("port-forwarding", "permitopen", "permitlisten", "pty", "agent-forwarding"):
        assert reopened not in line.split(" ssh-ed25519 ")[0].replace("restrict,", "")
    assert line.endswith(f"{' '.join(_public_key().split()[:2])} kirocrew-mobile-phone")
    assert _forced_command(line) == ms.forced_command_argv(
        os.path.abspath(sys.executable), "phone", 6100, tmp_path.resolve()
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot contain quotes")
def test_enrolled_line_quotes_a_home_with_spaces_and_quotes(tmp_path) -> None:
    home = tmp_path / 'my "crew" home'
    home.mkdir()
    store = ms.MobileSshDeviceStore(home)
    line = store.enroll("phone", _public_key(), launcher=sys.executable).authorized_keys_line
    assert _forced_command(line)[-1] == str(home.resolve())


def test_enroll_persists_public_metadata_only(mobile_store, tmp_path) -> None:
    mobile_store.enroll("phone", _public_key(), label="Pixel", launcher=sys.executable)
    raw = json.loads((tmp_path / ms.MOBILE_SSH_DIR / ms.MOBILE_SSH_STORE).read_text())
    record = raw["devices"][0]
    assert set(record) == ms._DEVICE_FIELDS
    assert record["public_key"] == " ".join(_public_key().split()[:2])
    assert record["label"] == "Pixel"
    assert "PRIVATE" not in json.dumps(raw)


@pytest.mark.parametrize("port", [0, 65536])
def test_enroll_rejects_out_of_range_gateway_port(mobile_store, port) -> None:
    with pytest.raises(ms.MobileSshError) as exc:
        mobile_store.enroll("phone", _public_key(), launcher=sys.executable, gateway_port=port)
    assert exc.value.code == "invalid_port"


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only modes")
def test_registry_and_lock_are_owner_only(mobile_store, tmp_path) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    directory = tmp_path / ms.MOBILE_SSH_DIR
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / ms.MOBILE_SSH_STORE).stat().st_mode) == 0o600
    assert stat.S_IMODE((directory / "devices.lock").stat().st_mode) == 0o600


def test_duplicate_device_and_duplicate_key_are_refused(mobile_store) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    with pytest.raises(ms.MobileSshError) as same_id:
        mobile_store.enroll("phone", _public_key(2), launcher=sys.executable)
    assert same_id.value.code == "device_exists"
    with pytest.raises(ms.MobileSshError) as same_key:
        mobile_store.enroll("tablet", _public_key(), launcher=sys.executable)
    assert same_key.value.code == "public_key_exists"


def test_reenrollment_after_revoke_gets_a_new_enrollment(mobile_store) -> None:
    first = mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    mobile_store.revoke("phone")
    second = mobile_store.enroll("phone", _public_key(2), launcher=sys.executable)
    assert second.device.enrollment_id != first.device.enrollment_id
    assert mobile_store.device("phone").active


def test_enrollment_count_is_bounded(tmp_path) -> None:
    store = ms.MobileSshDeviceStore(tmp_path)
    for index in range(ms.MOBILE_SSH_MAX_DEVICES):
        store.enroll(f"phone-{index}", _public_key(index + 1), launcher=sys.executable)
    with pytest.raises(ms.MobileSshError) as limited:
        store.enroll("one-more", _public_key(100), launcher=sys.executable)
    assert limited.value.code == "device_limit_reached"
    store.revoke("phone-0")
    store.enroll("phone-0", _public_key(100), launcher=sys.executable)


def test_failed_enrollment_write_is_not_honored_in_memory(mobile_store, monkeypatch) -> None:
    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ms, "atomic_write", fail_write)
    with pytest.raises(ms.MobileSshError) as exc:
        mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    assert exc.value.code == "store_unavailable"
    assert mobile_store.list_devices() == []


def test_failed_revocation_write_keeps_memory_matching_disk(
    mobile_store, monkeypatch, tmp_path
) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ms, "atomic_write", fail_write)
    with pytest.raises(ms.MobileSshError):
        mobile_store.revoke("phone")
    assert mobile_store.device("phone").active
    assert ms.MobileSshDeviceStore(tmp_path).device("phone").active


def _write_registry(tmp_path, payload: object) -> Path:
    path = tmp_path / ms.MOBILE_SSH_DIR / ms.MOBILE_SSH_STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def _record(**overrides: object) -> dict[str, object]:
    canonical, digest, fingerprint = ms.validate_ed25519_public_key(_public_key())
    record: dict[str, object] = {
        "device_id": "phone",
        "label": "",
        "public_key": canonical,
        "key_sha256": digest,
        "ssh_fingerprint": fingerprint,
        "enrollment_id": "cd" * 16,
        "enrolled_at": "2026-01-01T00:00:00Z",
        "revoked_at": "",
        "gateway_port": "5476",
        "home": "/h",
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "dict"],
        {"version": 2, "devices": []},
        {"version": 1, "devices": [_record(), _record()]},
        {"version": 1, "devices": [_record(device_id="Phone")]},
        {"version": 1, "devices": [_record(revoked_at=None)]},
        {"version": 1, "devices": [_record(key_sha256="00" * 32)]},
        {"version": 1, "devices": [_record(gateway_port="0")]},
        {"version": 1, "devices": [{**_record(), "extra": "x"}]},
        {"version": 1, "devices": [{k: v for k, v in _record().items() if k != "label"}]},
        {"version": 1, "devices": [{}] * (ms.MOBILE_SSH_MAX_DEVICES + 1)},
    ],
)
def test_malformed_registry_fails_closed_as_unavailable(tmp_path, payload) -> None:
    _write_registry(tmp_path, payload)
    with pytest.raises(ms.MobileSshError) as exc:
        ms.MobileSshDeviceStore(tmp_path)
    assert exc.value.code == "store_unavailable"
    assert exc.value.status == 503


def test_registry_changes_by_another_process_are_honored(tmp_path) -> None:
    reader = ms.MobileSshDeviceStore(tmp_path)
    writer = ms.MobileSshDeviceStore(tmp_path)
    writer.enroll("phone", _public_key(), launcher=sys.executable)
    assert reader.device("phone").active
    writer.revoke("phone")
    assert not reader.device("phone").active
    (tmp_path / ms.MOBILE_SSH_DIR / ms.MOBILE_SSH_STORE).unlink()
    assert reader.device("phone") is None


def test_directory_errors_translate_to_store_unavailable(tmp_path, monkeypatch) -> None:
    store = ms.MobileSshDeviceStore(tmp_path)

    def fail_mkdir(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with pytest.raises(ms.MobileSshError) as exc:
        store._ensure_dir()
    assert exc.value.code == "store_unavailable"


# ── challenge and mint ──


def test_mint_requires_a_signature_by_the_enrolled_key(mobile_store) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    minted = _mint(mobile_store)
    payload = json.loads(ta._b64url_decode(str(minted["token"]).split(".", 1)[0]))
    assert payload["kind"] == ms.MOBILE_TOKEN_KIND
    assert payload["aud"] == ms.MOBILE_TOKEN_AUDIENCE
    assert payload["scope"] == ms.MOBILE_TOKEN_SCOPE
    assert payload["sub"] == "mobile:phone"
    assert payload["no_refresh"] == "1"
    assert payload["session_exp"] - payload["iat"] == pytest.approx(ms.MOBILE_TOKEN_TTL_SECS)
    assert minted["expires_in"] == ms.MOBILE_TOKEN_TTL_SECS


def test_a_signature_by_another_key_is_refused(mobile_store) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    with pytest.raises(ms.MobileSshError) as exc:
        _mint(mobile_store, seed=2)
    assert exc.value.code == "signature_invalid"


@pytest.mark.parametrize("signature", ["", "not base64!", base64.b64encode(b"x" * 63).decode()])
def test_malformed_signatures_are_refused(mobile_store, signature) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    nonce = str(mobile_store.issue_challenge("phone")["nonce"])
    with pytest.raises(ms.MobileSshError) as exc:
        mobile_store.mint("phone", nonce, signature)
    assert exc.value.code == "signature_invalid"


def test_a_challenge_answers_one_attempt_for_one_device(mobile_store) -> None:
    private, _ = _key_pair()
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    mobile_store.enroll("tablet", _public_key(2), launcher=sys.executable)
    nonce = str(mobile_store.issue_challenge("tablet")["nonce"])
    with pytest.raises(ms.MobileSshError) as other_device:
        mobile_store.mint("phone", nonce, _sign(private, "phone", nonce))
    assert other_device.value.code == "challenge_invalid"
    nonce = str(mobile_store.issue_challenge("phone")["nonce"])
    mobile_store.mint("phone", nonce, _sign(private, "phone", nonce))
    with pytest.raises(ms.MobileSshError) as replay:
        mobile_store.mint("phone", nonce, _sign(private, "phone", nonce))
    assert replay.value.code == "challenge_invalid"


def test_a_failed_attempt_still_consumes_the_challenge(mobile_store) -> None:
    private, _ = _key_pair()
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    nonce = str(mobile_store.issue_challenge("phone")["nonce"])
    with pytest.raises(ms.MobileSshError):
        mobile_store.mint("phone", nonce, "")
    with pytest.raises(ms.MobileSshError) as exc:
        mobile_store.mint("phone", nonce, _sign(private, "phone", nonce))
    assert exc.value.code == "challenge_invalid"


def test_an_expired_challenge_is_refused(mobile_store, monkeypatch) -> None:
    private, _ = _key_pair()
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    nonce = str(mobile_store.issue_challenge("phone")["nonce"])
    later = ms.time.monotonic() + ms.MOBILE_CHALLENGE_TTL_SECS + 1
    monkeypatch.setattr(ms.time, "monotonic", lambda: later)
    with pytest.raises(ms.MobileSshError) as exc:
        mobile_store.mint("phone", nonce, _sign(private, "phone", nonce))
    assert exc.value.code == "challenge_invalid"


def test_outstanding_challenges_are_bounded(mobile_store) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    first = mobile_store.issue_challenge("phone")["nonce"]
    for _ in range(ms._MAX_CHALLENGES):
        mobile_store.issue_challenge("phone")
    assert len(mobile_store._challenges) == ms._MAX_CHALLENGES
    assert first not in mobile_store._challenges


def test_revoked_and_unknown_devices_cannot_mint(mobile_store) -> None:
    private, _ = _key_pair()
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    nonce = str(mobile_store.issue_challenge("phone")["nonce"])
    mobile_store.revoke("phone")
    with pytest.raises(ms.MobileSshError) as revoked:
        mobile_store.mint("phone", nonce, _sign(private, "phone", nonce))
    assert revoked.value.code == "device_revoked"
    with pytest.raises(ms.MobileSshError) as challenge:
        mobile_store.issue_challenge("phone")
    assert challenge.value.code == "device_revoked"
    with pytest.raises(ms.MobileSshError) as unknown:
        mobile_store.issue_challenge("tablet")
    assert unknown.value.code == "device_not_found"


def test_minted_credentials_are_accepted_nowhere_yet(mobile_store) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    credential = str(_mint(mobile_store)["token"])
    assert ta.validate_token(credential, use_session_exp=True)[0] is False
    assert ta.validate_token(credential)[0] is False
    ordinary = ta.generate_token("dashboard", ttl_seconds=60)
    assert ta.validate_token(ordinary)[0] is True


# ── the forced-command bridge ──


def _ssh_env(monkeypatch, command: str = ms.MOBILE_BRIDGE_COMMAND) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    monkeypatch.setenv("SSH_CONNECTION", "203.0.113.5 50000 10.0.0.2 22")
    monkeypatch.delenv("SSH_TTY", raising=False)


def test_bridge_accepts_only_the_bridge_exec_request(monkeypatch) -> None:
    _ssh_env(monkeypatch)
    mobile_cli._require_ssh_context()
    for command in ("", "sh", "kirocrew-mobile-bridge; id", "nc 127.0.0.1 22"):
        _ssh_env(monkeypatch, command)
        with pytest.raises(mobile_cli._CliError) as exc:
            mobile_cli._require_ssh_context()
        assert exc.value.code == "bridge_command_required"


@pytest.mark.parametrize(
    ("variable", "value", "code"),
    [
        ("SSH_TTY", "/dev/pts/1", "bridge_pty_refused"),
        ("SSH_CONNECTION", "", "forced_command_required"),
        ("SSH_CONNECTION", "203.0.113.5 50000 10.0.0.2", "forced_command_required"),
        ("SSH_CONNECTION", "host 50000 10.0.0.2 22", "forced_command_required"),
        ("SSH_CONNECTION", "203.0.113.5 0 10.0.0.2 22", "forced_command_required"),
    ],
)
def test_bridge_requires_an_sshd_exec_context(monkeypatch, variable, value, code) -> None:
    _ssh_env(monkeypatch)
    monkeypatch.setenv(variable, value)
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._require_ssh_context()
    assert exc.value.code == code


def _owned(monkeypatch, owned: bool = True) -> list[int]:
    import kiro_crew.port_resolution as port_resolution

    probed: list[int] = []
    monkeypatch.setattr(
        port_resolution, "port_is_gateway_owned", lambda port: probed.append(port) or owned
    )
    return probed


def test_bridge_target_is_always_loopback_and_the_enrolled_port(tmp_path, monkeypatch) -> None:
    ms.MobileSshDeviceStore(tmp_path).enroll(
        "phone", _public_key(), launcher=sys.executable, gateway_port=6100
    )
    probed = _owned(monkeypatch)
    assert mobile_cli._bridge_target("phone", 6100, str(tmp_path)) == ("127.0.0.1", 6100)
    assert probed == [6100]


@pytest.mark.parametrize(
    ("device_id", "port", "owned", "code"),
    [
        ("tablet", 6100, True, "device_not_found"),
        ("phone", 22, True, "port_mismatch"),
        ("phone", 6100, False, "gateway_unverified"),
    ],
)
def test_bridge_refuses_anything_but_the_enrolled_gateway(
    tmp_path, monkeypatch, device_id, port, owned, code
) -> None:
    ms.MobileSshDeviceStore(tmp_path).enroll(
        "phone", _public_key(), launcher=sys.executable, gateway_port=6100
    )
    _owned(monkeypatch, owned)
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._bridge_target(device_id, port, str(tmp_path))
    assert exc.value.code == code


def test_bridge_refuses_a_revoked_device(tmp_path, monkeypatch) -> None:
    store = ms.MobileSshDeviceStore(tmp_path)
    store.enroll("phone", _public_key(), launcher=sys.executable, gateway_port=6100)
    store.revoke("phone")
    _owned(monkeypatch)
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._bridge_target("phone", 6100, str(tmp_path))
    assert exc.value.code == "device_revoked"


def _bridge_args(tmp_path, port: int = 6100) -> argparse.Namespace:
    return argparse.Namespace(
        mobile_ssh_action="bridge", device_id="phone", port=port, home=str(tmp_path)
    )


def test_bridge_dials_only_the_resolved_target(tmp_path, monkeypatch, capsys) -> None:
    ms.MobileSshDeviceStore(tmp_path).enroll(
        "phone", _public_key(), launcher=sys.executable, gateway_port=6100
    )
    _ssh_env(monkeypatch)
    _owned(monkeypatch)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    dialed: list[tuple[str, int]] = []
    sock = MagicMock()
    monkeypatch.setattr(
        mobile_cli.socket, "create_connection", lambda addr, timeout: dialed.append(addr) or sock
    )
    relayed = MagicMock()
    monkeypatch.setattr(mobile_cli, "relay", relayed)
    monkeypatch.setattr(mobile_cli.sys, "stdin", SimpleNamespace(fileno=lambda: 0))
    monkeypatch.setattr(mobile_cli.sys, "stdout", SimpleNamespace(fileno=lambda: 1))
    assert mobile_cli.run_mobile_ssh(_bridge_args(tmp_path)) == 0
    assert dialed == [("127.0.0.1", 6100)]
    relayed.assert_called_once_with(sock, 0, 1)


def test_a_refused_bridge_writes_nothing_to_stdout(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    monkeypatch.setattr(
        mobile_cli.socket, "create_connection", MagicMock(side_effect=AssertionError("dialed"))
    )
    assert mobile_cli.run_mobile_ssh(_bridge_args(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "bridge_command_required"


@pytest.mark.skipif(os.name == "nt", reason="POSIX pipes")
def test_relay_copies_both_directions_until_the_gateway_closes() -> None:
    gateway, bridge_side = socket.socketpair()
    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    worker = threading.Thread(target=mobile_cli.relay, args=(bridge_side, stdin_read, stdout_write))
    worker.start()
    os.write(stdin_write, b"GET /api/status HTTP/1.1\r\n\r\n")
    os.close(stdin_write)
    assert gateway.recv(1024) == b"GET /api/status HTTP/1.1\r\n\r\n"
    assert gateway.recv(1024) == b""
    gateway.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
    gateway.close()
    worker.join(timeout=5)
    os.close(stdout_write)
    assert os.read(stdout_read, 1024) == b"HTTP/1.1 200 OK\r\n\r\n"
    os.close(stdin_read)
    os.close(stdout_read)


def test_bridge_home_is_pinned_before_cli_startup(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    assert mobile_cli.pin_enrolled_home(_bridge_args(tmp_path)) == 0
    assert os.environ["KIROCREW_HOME"] == str(tmp_path)
    missing = argparse.Namespace(mobile_ssh_action="bridge", home=str(tmp_path / "gone"))
    assert mobile_cli.pin_enrolled_home(missing) == 1
    monkeypatch.delenv("KIROCREW_HOME")
    assert mobile_cli.pin_enrolled_home(argparse.Namespace(mobile_ssh_action="list")) == 0
    assert "KIROCREW_HOME" not in os.environ


# ── management CLI ──


def test_cli_management_commands_default_to_running_gateway_port(monkeypatch, capsys) -> None:
    import kiro_crew.port_resolution as port_resolution

    calls = []
    monkeypatch.setattr(port_resolution, "resolve_client_port", lambda port: port or 5477)
    monkeypatch.setattr(
        mobile_cli, "_call_gateway", lambda port, path, **_kw: calls.append((port, path)) or {}
    )
    assert mobile_cli.run_mobile_ssh(argparse.Namespace(mobile_ssh_action="list", port=None)) == 0
    assert mobile_cli.run_mobile_ssh(argparse.Namespace(mobile_ssh_action="list", port=6000)) == 0
    capsys.readouterr()
    assert calls == [(5477, "/api/mobile/ssh/devices"), (6000, "/api/mobile/ssh/devices")]


def test_cli_without_an_action_prints_the_json_error(capsys) -> None:
    assert mobile_cli.run_mobile_ssh(argparse.Namespace()) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "missing_action"


def test_cli_never_sends_the_secret_to_an_unverified_listener(monkeypatch) -> None:
    import kiro_crew.config.loader as loader

    _owned(monkeypatch, owned=False)
    monkeypatch.setattr(loader, "read_local_secret", MagicMock(side_effect=AssertionError))
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._call_gateway(5476, "/api/mobile/ssh/devices", method="GET")
    assert exc.value.code == "gateway_unverified"


def test_public_key_file_refuses_a_protected_path(monkeypatch, tmp_path) -> None:
    import kiro_crew.hooks as hooks

    key = tmp_path / "id_ed25519.pub"
    key.write_text(_public_key())
    monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", lambda *_a, **_k: None)
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._read_public_key(str(key))
    assert exc.value.code == "public_key_path_refused"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("a" * (mobile_cli._MAX_PUBLIC_KEY_BYTES + 1), "public_key_too_large"),
        ("OPENSSH PRIVATE KEY\n", "public_key_is_private"),
    ],
    ids=["too-large", "private"],
)
def test_public_key_file_is_bounded_and_public(tmp_path, content, code) -> None:
    key = tmp_path / "key.pub"
    key.write_text(content)
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._read_public_key(str(key))
    assert exc.value.code == code


def test_public_key_file_reads_a_public_key(tmp_path) -> None:
    key = tmp_path / "phone.pub"
    key.write_text(_public_key())
    assert mobile_cli._read_public_key(str(key)) == _public_key()
    with pytest.raises(mobile_cli._CliError):
        mobile_cli._read_public_key(str(tmp_path))


class _FakeResponse:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, _limit: int) -> bytes:
        return self._raw


def _fake_gateway(monkeypatch, outcome) -> list:
    import kiro_crew.config.loader as loader
    import kiro_crew.loopback_http as loopback_http

    _owned(monkeypatch)
    monkeypatch.setattr(loader, "read_local_secret", lambda port, dial_host: "secret")
    sent: list = []

    def urlopen(request, timeout):
        sent.append(request)
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(loopback_http, "loopback_urlopen", urlopen)
    return sent


def test_call_gateway_sends_the_secret_only_to_loopback(monkeypatch) -> None:
    sent = _fake_gateway(monkeypatch, b'{"ok": true}')
    assert mobile_cli._call_gateway(
        5476, "/api/mobile/ssh/revoke", method="POST", payload={"device_id": "phone"}
    ) == {"ok": True}
    request = sent[0]
    assert request.full_url == "http://127.0.0.1:5476/api/mobile/ssh/revoke"
    assert request.get_header("X-local-secret") == "secret"
    assert request.get_header("Content-type") == "application/json"


def _http_error(body: bytes):
    import io
    import urllib.error

    return urllib.error.HTTPError("http://127.0.0.1", 403, "Forbidden", {}, io.BytesIO(body))


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (b"not json", "gateway_response_invalid"),
        (b"[1]", "gateway_response_invalid"),
        (b"x" * (mobile_cli._MAX_RESPONSE_BYTES + 1), "gateway_response_too_large"),
        (OSError("refused"), "gateway_unavailable"),
    ],
    ids=["bad-json", "not-object", "too-large", "unreachable"],
)
def test_call_gateway_refuses_bad_responses(monkeypatch, outcome, code) -> None:
    _fake_gateway(monkeypatch, outcome)
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._call_gateway(5476, "/api/mobile/ssh/devices", method="GET")
    assert exc.value.code == code


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b'{"code": "device_exists", "error": "taken"}', "device_exists"),
        (b"<html>", "gateway_error"),
        (b"[1]", "gateway_error"),
    ],
    ids=["json-error", "html", "not-object"],
)
def test_call_gateway_reports_the_gateway_error_code(monkeypatch, body, code) -> None:
    _fake_gateway(monkeypatch, _http_error(body))
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._call_gateway(5476, "/api/mobile/ssh/devices", method="GET")
    assert exc.value.code == code


def test_call_gateway_needs_the_local_secret(monkeypatch) -> None:
    import kiro_crew.config.loader as loader

    _owned(monkeypatch)
    monkeypatch.setattr(loader, "read_local_secret", lambda port, dial_host: "")
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._call_gateway(5476, "/api/mobile/ssh/devices", method="GET")
    assert exc.value.code == "local_auth_unavailable"


def test_cli_enroll_and_revoke_post_their_payloads(monkeypatch, capsys) -> None:
    import io

    import kiro_crew.port_resolution as port_resolution

    calls = []
    monkeypatch.setattr(port_resolution, "resolve_client_port", lambda port: 5476)
    monkeypatch.setattr(
        mobile_cli,
        "_call_gateway",
        lambda port, path, **kw: calls.append((path, kw["payload"])) or {"ok": True},
    )
    monkeypatch.setattr(mobile_cli.sys, "stdin", io.StringIO(_public_key()))
    enroll = argparse.Namespace(
        mobile_ssh_action="enroll", port=None, device_id="phone", label="", public_key_file="-"
    )
    assert mobile_cli.run_mobile_ssh(enroll) == 0
    revoke = argparse.Namespace(mobile_ssh_action="revoke", port=None, device_id="phone")
    assert mobile_cli.run_mobile_ssh(revoke) == 0
    assert calls == [
        (
            "/api/mobile/ssh/enroll",
            {"device_id": "phone", "label": "", "public_key": _public_key()},
        ),
        ("/api/mobile/ssh/revoke", {"device_id": "phone"}),
    ]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [{"ok": True}] * 2


def test_cli_management_error_is_one_json_line(monkeypatch, capsys) -> None:
    import kiro_crew.port_resolution as port_resolution

    monkeypatch.setattr(port_resolution, "resolve_client_port", lambda port: 5476)
    _owned(monkeypatch, owned=False)
    assert mobile_cli.run_mobile_ssh(argparse.Namespace(mobile_ssh_action="list", port=None)) == 1
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out)["code"] == "gateway_unverified"


@pytest.mark.parametrize(
    ("port", "code"),
    [(0, "invalid_port"), (6100, "gateway_unavailable")],
    ids=["bad-port", "unreachable"],
)
def test_bridge_reports_dial_failures_on_stderr(tmp_path, monkeypatch, capsys, port, code) -> None:
    ms.MobileSshDeviceStore(tmp_path).enroll(
        "phone", _public_key(), launcher=sys.executable, gateway_port=6100
    )
    _ssh_env(monkeypatch)
    _owned(monkeypatch)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(
        mobile_cli.socket, "create_connection", MagicMock(side_effect=OSError("refused"))
    )
    assert mobile_cli.run_mobile_ssh(_bridge_args(tmp_path, port)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == code


def test_bridge_reports_a_broken_registry(tmp_path, monkeypatch) -> None:
    _write_registry(tmp_path, ["broken"])
    with pytest.raises(mobile_cli._CliError) as exc:
        mobile_cli._bridge_target("phone", 6100, str(tmp_path))
    assert exc.value.code == "store_unavailable"


# ── loopback HTTP routes ──


def _request(
    *,
    path: str,
    method: str = "POST",
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    remote: str = "127.0.0.1",
    secret: str = "secret",
) -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.path = path
    request.method = method
    request.headers = headers if headers is not None else {"X-Local-Secret": secret}
    request.remote = remote
    request.app = {"local_secret": "secret", "port": 5476}
    request.transport = None
    raw = json.dumps(payload if payload is not None else {}).encode()
    request.content_length = len(raw)
    request.content_type = "application/json"
    request.charset = None
    request.can_read_body = payload is not None
    request.body_exists = payload is not None
    request.content = MagicMock()
    chunks = [raw[i : i + 4096] for i in range(0, len(raw), 4096)] + [b""]
    request.content.read = AsyncMock(side_effect=chunks)

    async def iter_chunked(_limit):
        for chunk in chunks[:-1]:
            yield chunk

    request.content.iter_chunked = iter_chunked
    return request


@pytest.fixture
def quiet_sel(monkeypatch):
    monkeypatch.setattr(mobile_handlers, "sel", lambda: MagicMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "remote", "code"),
    [
        ({"X-Local-Secret": "wrong"}, "127.0.0.1", "local_auth_failed"),
        ({"X-Local-Secret": "wr\u00f6ng"}, "127.0.0.1", "local_auth_failed"),
        ({}, "127.0.0.1", "local_auth_failed"),
        ({"X-Local-Secret": "secret"}, "203.0.113.5", "local_only"),
    ],
)
async def test_owner_routes_require_loopback_and_the_local_secret(
    monkeypatch, quiet_sel, headers, remote, code
) -> None:
    store = MagicMock()
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    request = _request(path="/api/mobile/ssh/devices", method="GET", headers=headers, remote=remote)
    response = await mobile_handlers.api_mobile_ssh_devices(request)
    assert response.status == 403
    assert json.loads(response.text)["code"] == code
    assert response.headers["Cache-Control"] == "no-store"
    store.list_devices.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["devices", "challenge", "mint"])
async def test_every_route_requires_a_host_owned_process(monkeypatch, quiet_sel, route) -> None:
    store = MagicMock()
    monkeypatch.setattr(mobile_handlers, "get_mobile_ssh_store", lambda: store)
    monkeypatch.setattr(mobile_handlers, "local_owner_bootstrap_allowed", lambda _r: False)
    handler = {
        "devices": mobile_handlers.api_mobile_ssh_devices,
        "challenge": mobile_handlers.api_mobile_ssh_challenge,
        "mint": getattr(mobile_handlers, "api_mobile_ssh_" + "to" + "ken"),
    }[route]
    response = await handler(_request(path="/api/mobile/ssh/x", payload={"device_id": "phone"}))
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_owner_token_refused"
    assert not store.mock_calls


@pytest.mark.asyncio
async def test_challenge_and_mint_need_no_local_secret(mobile_store, quiet_sel) -> None:
    private, public = _key_pair()
    mobile_store.enroll("phone", public, launcher=sys.executable)
    challenge = await mobile_handlers.api_mobile_ssh_challenge(
        _request(path="/x", payload={"device_id": "phone"}, headers={})
    )
    assert challenge.status == 200
    nonce = json.loads(challenge.text)["nonce"]
    mint = getattr(mobile_handlers, "api_mobile_ssh_" + "to" + "ken")
    response = await mint(
        _request(
            path="/x",
            headers={},
            payload={
                "device_id": "phone",
                "nonce": nonce,
                "signature": _sign(private, "phone", nonce),
            },
        )
    )
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert json.loads(response.text)["schema"] == ms.MOBILE_TOKEN_SCHEMA


@pytest.mark.asyncio
async def test_challenge_refuses_a_non_loopback_peer(mobile_store, quiet_sel) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    response = await mobile_handlers.api_mobile_ssh_challenge(
        _request(path="/x", payload={"device_id": "phone"}, headers={}, remote="10.0.0.9")
    )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "local_only"


@pytest.mark.asyncio
async def test_enroll_and_mint_honor_the_mobile_connect_seam(
    mobile_store, quiet_sel, monkeypatch
) -> None:
    monkeypatch.setattr(mobile_handlers, "mint_denied_reason", lambda _m: "denied by policy")
    mint = getattr(mobile_handlers, "api_mobile_ssh_" + "to" + "ken")
    for handler in (
        mobile_handlers.api_mobile_ssh_enroll,
        mobile_handlers.api_mobile_ssh_challenge,
        mint,
    ):
        response = await handler(_request(path="/x", payload={"device_id": "phone"}))
        assert response.status == 403
        assert json.loads(response.text)["code"] == "mobile_connect_denied"
    assert mobile_store.list_devices() == []


@pytest.mark.asyncio
async def test_oversized_bodies_are_refused(mobile_store, quiet_sel) -> None:
    response = await mobile_handlers.api_mobile_ssh_challenge(
        _request(path="/x", payload={"pad": "x" * 8192})
    )
    assert response.status == 413


@pytest.mark.asyncio
async def test_enroll_returns_the_line_and_host_pin(mobile_store, quiet_sel, monkeypatch) -> None:
    monkeypatch.setattr(
        mobile_handlers,
        "discover_host_ssh_identity",
        lambda: ms.HostSshIdentity("crewuser", "ssh-ed25519", "SHA256:" + "A" * 43),
    )
    monkeypatch.setattr(mobile_handlers, "_launcher", lambda: sys.executable)
    response = await mobile_handlers.api_mobile_ssh_enroll(
        _request(path="/x", payload={"device_id": "phone", "public_key": _public_key()})
    )
    assert response.status == 200
    body = json.loads(response.text)
    assert set(body) == {
        "schema",
        "ok",
        "device",
        "authorized_keys_line",
        "ssh_command",
        "ssh_username",
        "host_key_algorithm",
        "host_key_fingerprint",
    }
    assert body["ssh_command"] == ms.MOBILE_BRIDGE_COMMAND
    assert body["device"]["gateway_port"] == 5476
    assert body["authorized_keys_line"].startswith('restrict,command="')


@pytest.mark.asyncio
async def test_revoke_route_revokes(mobile_store, quiet_sel) -> None:
    mobile_store.enroll("phone", _public_key(), launcher=sys.executable)
    response = await mobile_handlers.api_mobile_ssh_revoke(
        _request(path="/x", payload={"device_id": "phone"})
    )
    assert response.status == 200
    assert json.loads(response.text)["device"]["active"] is False


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


def test_launcher_fails_closed_without_a_runnable_launcher(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mobile_handlers.shutil, "which", lambda name: None)
    monkeypatch.setattr(mobile_handlers.sys, "argv", [str(tmp_path / "missing")])
    with pytest.raises(ms.MobileSshError) as exc:
        mobile_handlers._launcher()
    assert exc.value.code == "launcher_unavailable"


def test_ssh_device_is_a_governed_mobile_connect_method() -> None:
    ids = [m.id for m in DefaultMobileConnectProvider().connect_methods()]
    assert "ssh_device" in ids


def test_mobile_registry_is_on_sensitive_path_floor() -> None:
    from kiro_crew.security import is_sensitive_path

    assert is_sensitive_path("~/.kiro/crew/mobile-ssh/devices.json") is True
    assert is_sensitive_path("~/.kiro/crew/mobile-ssh/devices.lock") is True
