"""Tests for kiro_crew.secrets.vault — encrypted vault store."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from kiro_crew.secrets.vault import SecretValue, SecretVault


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Provide a temporary config directory for vault tests."""
    return tmp_path / "config"


@pytest.fixture
def vault(vault_dir: Path) -> SecretVault:
    """Provide a SecretVault instance."""
    return SecretVault(vault_dir)


# ── Roundtrip ──


@pytest.mark.asyncio
async def test_set_get_roundtrip(vault: SecretVault) -> None:
    """set() then get() returns the original value."""
    await vault.set("my_token", "hunter2")
    result = vault.get("my_token")
    assert result is not None
    assert result.reveal() == "hunter2"


@pytest.mark.asyncio
async def test_list_names(vault: SecretVault) -> None:
    """list_names() returns all stored keys."""
    await vault.set("alpha", "a")
    await vault.set("beta", "b")
    await vault.set("gamma", "c")
    names = vault.list_names()
    assert sorted(names) == ["alpha", "beta", "gamma"]


@pytest.mark.asyncio
async def test_delete(vault: SecretVault) -> None:
    """delete() removes a secret; get() returns None afterward."""
    await vault.set("ephemeral", "bye")
    assert vault.get("ephemeral") is not None
    await vault.delete("ephemeral")
    assert vault.get("ephemeral") is None


@pytest.mark.asyncio
async def test_delete_fresh_vault_is_noop(vault: SecretVault) -> None:
    """delete() on a fresh vault (no store file) is a no-op."""
    await vault.delete("nonexistent")
    assert vault.list_names() == []


# ── AAD binding (invariant I5) ──


@pytest.mark.asyncio
async def test_aad_binding(vault: SecretVault, vault_dir: Path) -> None:
    """Transplanting ciphertext from entry A to entry B is detected."""
    await vault.set("A", "secret_A")

    store_path = vault_dir / ".vault" / "secrets.enc"
    raw = json.loads(store_path.read_text(encoding="utf-8"))

    # Transplant A's ciphertext to a new entry named B.
    raw["entries"]["B"] = raw["entries"]["A"]
    store_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(Exception):
        # Decryption under B's AAD must fail (InvalidTag or similar).
        vault.get("B")


# ── Cross-instance visibility ──


@pytest.mark.asyncio
async def test_second_instance_sees_writes(vault: SecretVault, vault_dir: Path) -> None:
    """A second vault instance reads entries written by the first."""
    await vault.set("key1", "value1")

    vault2 = SecretVault(vault_dir)
    await vault2.set("key2", "value2")

    # First instance re-reads from disk on every get (no stale cache).
    assert vault.get("key1") is not None
    assert vault.get("key2") is not None


# ── SecretValue opacity (invariant I6) ──


def test_secret_value_opacity() -> None:
    """SecretValue never reveals plaintext in repr/str."""
    sv = SecretValue("super_secret")
    assert repr(sv) == "SecretValue(****)"
    assert str(sv) == "****"
    assert sv.reveal() == "super_secret"
    assert "super_secret" not in repr(sv)
    assert "super_secret" not in str(sv)

    # Equality compares revealed values.
    assert SecretValue("x") == SecretValue("x")
    assert SecretValue("x") != SecretValue("y")

    # Not hashable.
    with pytest.raises(TypeError):
        hash(sv)


# ── Agent denylist (invariant I2) ──


def test_denylist_coverage() -> None:
    """The .vault directory is in the agent denylist."""
    from kiro_crew.security import _CREW_SECRET_LEAVES

    assert ".vault" in _CREW_SECRET_LEAVES


# ── Atomic write ──


@pytest.mark.asyncio
async def test_atomic_write(vault: SecretVault, vault_dir: Path) -> None:
    """Concurrent writes do not corrupt the store."""

    async def writer(name: str, value: str) -> None:
        await vault.set(name, value)

    tasks = [writer(f"key_{i}", f"val_{i}") for i in range(20)]
    await asyncio.gather(*tasks)

    for i in range(20):
        result = vault.get(f"key_{i}")
        assert result is not None
        assert result.reveal() == f"val_{i}"


def test_mixed_key_guard(tmp_path):
    """Refuses to create a new key when secrets.enc already exists."""
    vault = SecretVault(tmp_path)
    (tmp_path / ".vault").mkdir(exist_ok=True)
    store = tmp_path / ".vault" / "secrets.enc"
    store.write_text('{"version":1,"backend":"file","entries":{}}')
    with pytest.raises(ValueError, match="Cannot create a new key"):
        vault._get_or_create_key()


def test_backend_mismatch_rejected(tmp_path):
    """Vault refuses a store with wrong backend field."""
    vault = SecretVault(tmp_path)
    (tmp_path / ".vault").mkdir(exist_ok=True)
    key = os.urandom(32)
    (tmp_path / ".vault" / ".vault_key").write_bytes(key)
    store_data = {"version": 1, "backend": "wrong-backend", "entries": {}}
    (tmp_path / ".vault" / "secrets.enc").write_text(json.dumps(store_data))
    with pytest.raises(ValueError, match="backend mismatch"):
        vault._load_entries()


def test_short_write_raises(tmp_path, monkeypatch):
    """Short write during key creation raises OSError."""
    vault = SecretVault(tmp_path)

    def short_write(fd, data):
        return len(data) - 1

    monkeypatch.setattr(os, "write", short_write)
    with pytest.raises(OSError, match="Short write"):
        vault._get_or_create_key()


def test_eq_not_implemented():
    """SecretValue.__eq__ returns NotImplemented for non-SecretValue."""
    sv = SecretValue("hello")
    assert sv.__eq__("hello") is NotImplemented
    assert sv != "hello"


def test_restrict_to_owner_called_on_read(tmp_path, monkeypatch):
    """_get_or_create_key calls restrict_to_owner on existing key file."""
    vault = SecretVault(tmp_path)
    (tmp_path / ".vault").mkdir(exist_ok=True)
    key_path = tmp_path / ".vault" / ".vault_key"
    key_path.write_bytes(os.urandom(32))
    os.chmod(str(key_path), 0o600)

    calls = []
    monkeypatch.setattr(
        "kiro_crew.secrets.vault.restrict_to_owner",
        lambda path: calls.append(str(path)),
    )
    vault._get_or_create_key()
    assert len(calls) == 1
    assert ".vault_key" in calls[0]


# ── Agent isolation: the .vault keystone leaf denies same-UID reads ──
#
# GPT 5.6 review flagged (vault.py:217): a prompt-injected agent running as the
# same UID can `import SecretVault` / `open('.vault/...')` and read plaintext,
# so "revert until .vault is hidden by every agent OS sandbox". This is a false
# positive for the Kiro Crew agent path: `.vault` is registered as a keystone
# leaf in `security._CREW_SECRET_LEAVES`, expanded into `_SENSITIVE_HOME_DIRS`,
# and enforced by the verb-independent `is_sensitive_path` backstop that every
# agent file-access surface (hooks.on_tool_call, validate_file_path, artifacts,
# dashboard file I/O, knowledge indexing) routes through — including a scripted
# `python -c "open('~/.kiro/crew/.vault/...')"`. These tests prove that narrower
# scope is sufficient: the OS-mediated control already denies the exact vectors
# the finding describes, so no in-process guard (the FP-rejected theater) is
# needed here and no revert is warranted.


def test_vault_dir_is_a_registered_keystone_leaf() -> None:
    """The `.vault` directory is a keystone leaf in security._CREW_SECRET_LEAVES."""
    from kiro_crew import security

    assert ".vault" in security._CREW_SECRET_LEAVES


def test_keystone_denies_agent_reads_of_the_vault(tmp_path, monkeypatch) -> None:
    """is_sensitive_path() denies every agent-mediated read of a .vault path.

    Covers the exact vectors GPT flagged: the AES key file, the ciphertext
    store, and a scripted open() of an arbitrary file under .vault. Anchor a
    crew home via KIROCREW_HOME so the keystone-leaf expansion applies, then
    assert the enforced predicate returns True for each.
    """
    from kiro_crew import security

    crew_home = tmp_path / "crew"
    (crew_home / ".vault").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))

    # The vault writes secrets.enc + .vault_key under <config_dir>/.vault.
    vault_dir = crew_home / ".vault"
    for leaf in (".vault_key", "secrets.enc", ".secrets.enc.lock"):
        target = vault_dir / leaf
        assert security.is_sensitive_path(
            str(target)
        ), f"{leaf} under .vault must be denied to the agent by the keystone"

    # The scripted `python -c "open('.vault/anything')"` vector from the finding.
    assert security.is_sensitive_path(str(vault_dir / "anything.txt"))
    # The directory itself is protected.
    assert security.is_sensitive_path(str(vault_dir))


def test_keystone_allows_a_non_vault_sibling(tmp_path, monkeypatch) -> None:
    """Negative control: a sibling path outside .vault is NOT denied.

    Guards against the assertion above passing because is_sensitive_path()
    returns True for everything under the crew home.
    """
    from kiro_crew import security

    crew_home = tmp_path / "crew"
    (crew_home / ".vault").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))

    assert not security.is_sensitive_path(str(crew_home / "notes" / "todo.txt"))


def test_vault_dir_is_hidden_by_the_os_sandbox() -> None:
    """The .vault dir is bind-mount-hidden from agent subprocesses in every mode.

    GPT 5.6 correctly noted the keystone (`is_sensitive_path`) gates the agent's
    in-process tool calls, but a spawned `python -c "import SecretVault; ...get()"`
    subprocess does a raw OS open() that bypasses that gate. `sandbox.py` hides
    sensitive dirs from the subprocess tree via bind-mount (Linux) / seatbelt
    (macOS); the vault dir must be in every mode's list so the subprocess cannot
    read `.vault/.vault_key` and decrypt.
    """
    from kiro_crew import sandbox

    for mode_list in (
        sandbox._STRICT_DIRS,
        sandbox._STANDARD_DIRS,
        sandbox._CC_DIRS,
    ):
        assert (
            ".kiro/crew/.vault" in mode_list
        ), "the vault dir must be OS-sandbox-hidden in every mode"
        assert ".kirocrew/.vault" in mode_list, "the legacy vault dir path must also be hidden"
