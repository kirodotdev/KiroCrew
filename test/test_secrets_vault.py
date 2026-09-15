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


# ── get_many: single-load batch read ──


@pytest.mark.asyncio
async def test_get_many_roundtrip(vault: SecretVault) -> None:
    """get_many returns SecretValues for present names and None for absent."""
    await vault.set("A", "va")
    await vault.set("B", "vb")

    result = vault.get_many(["A", "B", "MISSING"])
    assert result["A"] is not None and result["A"].reveal() == "va"
    assert result["B"] is not None and result["B"].reveal() == "vb"
    assert result["MISSING"] is None


@pytest.mark.asyncio
async def test_get_many_empty_list(vault: SecretVault) -> None:
    """get_many([]) returns an empty mapping and reads nothing."""
    await vault.set("A", "va")
    assert vault.get_many([]) == {}


def test_get_many_loads_store_and_key_once(tmp_path, monkeypatch) -> None:
    """get_many reads secrets.enc once and the key file once for K names."""
    vault = SecretVault(tmp_path)
    vault._set_sync("A", "va")
    vault._set_sync("B", "vb")
    vault._set_sync("C", "vc")

    load_calls = 0
    key_calls = 0
    orig_load = SecretVault._load_entries
    orig_key = SecretVault._get_or_create_key

    def counting_load(self):
        nonlocal load_calls
        load_calls += 1
        return orig_load(self)

    def counting_key(self):
        nonlocal key_calls
        key_calls += 1
        return orig_key(self)

    monkeypatch.setattr(SecretVault, "_load_entries", counting_load)
    monkeypatch.setattr(SecretVault, "_get_or_create_key", counting_key)

    result = vault.get_many(["A", "B", "C"])
    assert {k: v.reveal() for k, v in result.items() if v} == {
        "A": "va",
        "B": "vb",
        "C": "vc",
    }
    assert load_calls == 1
    assert key_calls == 1


def test_get_many_all_missing_does_not_create_key(tmp_path) -> None:
    """get_many for names none of which exist does not create a key file.

    Matches get()'s behaviour (returns before _get_or_create_key for a missing
    name), so a spawn that references only absent secrets on a fresh vault does
    not materialise vault key state.
    """
    vault = SecretVault(tmp_path)
    result = vault.get_many(["NOPE", "ALSO_NOPE"])
    assert result == {"NOPE": None, "ALSO_NOPE": None}
    assert not (tmp_path / ".vault" / ".vault_key").exists()


# ── Corrupt-store error contract ──


def _seed_key(tmp_path: Path) -> None:
    (tmp_path / ".vault").mkdir(exist_ok=True)
    (tmp_path / ".vault" / ".vault_key").write_bytes(os.urandom(32))
    os.chmod(str(tmp_path / ".vault" / ".vault_key"), 0o600)


def _write_store(tmp_path: Path, entries) -> None:
    store = {"version": 1, "backend": "file", "entries": entries}
    (tmp_path / ".vault" / "secrets.enc").write_text(json.dumps(store))


def test_corrupt_entries_not_a_dict_raises(tmp_path) -> None:
    """A non-mapping 'entries' fails closed with a descriptive ValueError."""
    _seed_key(tmp_path)
    _write_store(tmp_path, ["not", "a", "dict"])
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError, match="Vault store corrupt: 'entries' must be an object"):
        vault._load_entries()


def test_corrupt_string_entry_raises_from_get(tmp_path) -> None:
    """A string entry (not a dict) raises a descriptive ValueError from get()."""
    _seed_key(tmp_path)
    _write_store(tmp_path, {"A": "i-am-not-an-entry-dict"})
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError, match="Vault store corrupt: an entry is malformed"):
        vault.get("A")


def test_corrupt_null_entry_is_malformed_not_missing_in_get_many(tmp_path) -> None:
    """A JSON ``null`` entry raises corrupt-store, not a missing-name ``None``.

    ``entries.get(name)`` would fold a corrupt ``null`` value into the
    missing-name branch, telling the operator to store a secret that IS in the
    (corrupt) store. Membership is checked first so the null reaches
    ``_decrypt_entry`` and fails closed with the corrupt-store message.
    """
    _seed_key(tmp_path)
    _write_store(tmp_path, {"A\u202eEVIL": None})
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError) as exc:
        vault.get_many(["A\u202eEVIL"])
    msg = str(exc.value)
    assert "Vault store corrupt: an entry is malformed" in msg
    # The sink rule holds one layer down too: the caller-supplied entry name
    # (which may carry injection-capable characters) never appears in the
    # error text.
    assert "EVIL" not in msg
    assert "\u202e" not in msg


def test_corrupt_missing_nonce_raises_from_get(tmp_path) -> None:
    """An entry missing 'nonce' raises a descriptive ValueError."""
    _seed_key(tmp_path)
    _write_store(tmp_path, {"A": {"ct": "abcd"}})
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError, match="Vault store corrupt: an entry is malformed"):
        vault.get("A")


def test_corrupt_bad_hex_raises_from_get(tmp_path) -> None:
    """An entry with non-hex nonce/ct raises a descriptive ValueError."""
    _seed_key(tmp_path)
    _write_store(tmp_path, {"A": {"nonce": "zzzz", "ct": "zzzz"}})
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError, match="Vault store corrupt: an entry is malformed"):
        vault.get("A")


def test_corrupt_error_does_not_leak_material(tmp_path) -> None:
    """The corrupt-entry error carries no ciphertext/plaintext material."""
    _seed_key(tmp_path)
    secret_ct = "deadbeefdeadbeefdeadbeef"
    _write_store(tmp_path, {"A": {"nonce": "zz", "ct": secret_ct}})
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError) as exc:
        vault.get("A")
    assert secret_ct not in str(exc.value)


def test_corrupt_entry_raises_from_get_many(tmp_path) -> None:
    """get_many surfaces the same descriptive ValueError on a corrupt entry."""
    _seed_key(tmp_path)
    _write_store(tmp_path, {"A": {"nonce": "zzzz", "ct": "zzzz"}})
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError, match="Vault store corrupt: an entry is malformed"):
        vault.get_many(["A"])


# ── Non-dict top-level envelope guard (F5) ──


def _write_raw_store(tmp_path: Path, raw_json: str) -> None:
    """Write raw JSON bytes directly to the store path, bypassing the envelope builder."""
    (tmp_path / ".vault").mkdir(exist_ok=True)
    (tmp_path / ".vault" / "secrets.enc").write_text(raw_json, encoding="utf-8")


@pytest.mark.parametrize(
    "raw",
    [
        "[]",  # JSON array
        '["a","b"]',  # JSON array with content
        '"x"',  # JSON string
        "42",  # JSON number
        "true",  # JSON boolean
        "null",  # JSON null
    ],
)
def test_non_dict_top_level_envelope_raises_value_error(tmp_path, raw) -> None:
    """A store whose top-level JSON value is not an object fails closed with ValueError.

    On origin/main (without the isinstance guard) this raised a raw AttributeError
    from envelope.get().  The fix raises a descriptive ValueError that mirrors the
    existing 'entries' guard, so callers see a consistent module-level error rather
    than an internal implementation detail.
    """
    _seed_key(tmp_path)
    _write_raw_store(tmp_path, raw)
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError, match="corrupt"):
        vault._load_entries()


def test_non_dict_envelope_error_contains_type_name(tmp_path) -> None:
    """The ValueError message names the unexpected type, not the store content."""
    _seed_key(tmp_path)
    _write_raw_store(tmp_path, "[]")
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError) as exc:
        vault._load_entries()
    msg = str(exc.value)
    # Message must mention "object" (expected) and the actual type.
    assert "object" in msg
    assert "list" in msg


def test_non_dict_envelope_error_does_not_echo_store_content(tmp_path) -> None:
    """The corrupt-envelope error carries no store content in the message."""
    _seed_key(tmp_path)
    store_payload = '"sensitive-payload-12345"'
    _write_raw_store(tmp_path, store_payload)
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError) as exc:
        vault._load_entries()
    assert "sensitive-payload-12345" not in str(exc.value)


def test_non_dict_envelope_raises_from_list_names(tmp_path) -> None:
    """The public list_names() path surfaces the ValueError for a non-dict envelope."""
    _seed_key(tmp_path)
    _write_raw_store(tmp_path, "[]")
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError, match="corrupt"):
        vault.list_names()


def test_valid_store_still_loads_after_guard(tmp_path) -> None:
    """A well-formed store is unaffected by the isinstance guard."""
    vault = SecretVault(tmp_path)
    vault._set_sync("k", "v")
    # Must not raise — guard is only a speed-bump for non-dict values.
    entries = vault._load_entries()
    assert "k" in entries


def test_corrupt_entries_guard_still_works_with_envelope_guard(tmp_path) -> None:
    """The existing 'entries' guard (non-dict entries value) still fires correctly.

    Ensures the new top-level guard does not mask or accidentally break the
    entries-level guard that was already present.
    """
    _seed_key(tmp_path)
    _write_store(tmp_path, ["not", "a", "dict"])
    vault = SecretVault(tmp_path)
    with pytest.raises(ValueError, match="Vault store corrupt: 'entries' must be an object"):
        vault._load_entries()
