"""Connected computers: the vault-backed JSON document fails closed and never loses a write.

A pure-Python vault double stands in for :class:`SecretVault` so each failure
(unreadable entry, oversized or malformed text, a write that keeps losing the
compare-and-swap) is reached exactly; one round trip runs on the real vault.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from kiro_crew.connected_computers import vault_doc
from kiro_crew.connected_computers.vault_doc import (
    DOC_VERSION,
    MAX_DOC_BYTES,
    VaultDocError,
    VaultJsonDoc,
)
from kiro_crew.secrets.vault import SecretVault

NAME = "CONNECTED_COMPUTERS_TEST"


class _Value:
    def __init__(self, text: str) -> None:
        self._text = text

    def reveal(self) -> str:
        return self._text


class _Vault:
    """One entry with a revision; ``losses`` writes lose the race before any lands."""

    def __init__(self, text: Optional[str] = None, *, losses: int = 0) -> None:
        self.text = text
        self.revision: Optional[str] = None if text is None else "r0"
        self.losses = losses
        self.attempts = 0
        self.get_error: Optional[BaseException] = None
        self.set_error: Optional[BaseException] = None

    def get_with_revision(self, name: str) -> Any:
        assert name == NAME
        if self.get_error is not None:
            raise self.get_error
        if self.text is None:
            return None
        return _Value(self.text), self.revision

    def set_if_revision_sync(self, name: str, value: str, revision: Optional[str]) -> Any:
        self.attempts += 1
        if self.set_error is not None:
            raise self.set_error
        if self.losses:
            self.losses -= 1
            return None
        if revision != self.revision:
            return None
        self.text = value
        self.revision = f"r{self.attempts}"
        return self.revision


def _doc(vault: _Vault) -> VaultJsonDoc:
    return VaultJsonDoc(vault, NAME)


class TestRead:
    def test_a_never_written_document_is_empty_at_the_current_version(self):
        assert _doc(_Vault()).read() == {"version": DOC_VERSION}

    def test_a_written_document_reads_back(self):
        stored = {"version": DOC_VERSION, "devices": {"a": 1}}
        assert _doc(_Vault(json.dumps(stored))).read() == stored

    def test_an_unreadable_vault_is_an_error_not_an_empty_document(self):
        vault = _Vault()
        vault.get_error = ValueError("bad key")
        with pytest.raises(VaultDocError, match="unreadable") as exc:
            _doc(vault).read()
        assert isinstance(exc.value.__cause__, ValueError)

    def test_an_oversized_entry_is_refused(self):
        with pytest.raises(VaultDocError, match="too large"):
            _doc(_Vault("x" * (MAX_DOC_BYTES + 1))).read()

    def test_text_that_is_not_json_is_unreadable(self):
        with pytest.raises(VaultDocError, match="unreadable"):
            _doc(_Vault("{not json")).read()

    @pytest.mark.parametrize("text", ["[1]", '"text"', "{}", '{"version": 2}'])
    def test_a_document_of_another_shape_or_version_is_invalid(self, text):
        with pytest.raises(VaultDocError, match="invalid"):
            _doc(_Vault(text)).read()


class TestUpdate:
    def test_the_first_write_creates_the_entry(self):
        vault = _Vault()

        def add(doc: dict[str, Any]) -> str:
            doc["devices"] = {"laptop": 1}
            return "added"

        assert _doc(vault).update(add) == "added"
        assert json.loads(vault.text or "") == {"version": DOC_VERSION, "devices": {"laptop": 1}}

    def test_a_lost_race_rereads_and_reapplies(self):
        vault = _Vault(json.dumps({"version": DOC_VERSION, "n": 1}), losses=2)
        seen: list[int] = []

        def bump(doc: dict[str, Any]) -> int:
            seen.append(doc["n"])
            doc["n"] += 1
            return doc["n"]

        assert _doc(vault).update(bump) == 2
        assert seen == [1, 1, 1]
        assert json.loads(vault.text or "")["n"] == 2

    def test_a_write_that_never_lands_gives_up_by_name(self):
        vault = _Vault(losses=vault_doc._CAS_ATTEMPTS)
        calls: list[int] = []
        with pytest.raises(VaultDocError, match="kept changing"):
            _doc(vault).update(lambda doc: calls.append(1))
        assert len(calls) == vault_doc._CAS_ATTEMPTS == vault.attempts
        assert vault.text is None

    def test_a_failing_write_is_one_error_class(self):
        vault = _Vault()
        vault.set_error = OSError("disk full")
        with pytest.raises(VaultDocError, match="could not be saved") as exc:
            _doc(vault).update(lambda doc: None)
        assert isinstance(exc.value.__cause__, OSError)

    def test_a_document_grown_too_large_is_never_written(self):
        vault = _Vault()

        def grow(doc: dict[str, Any]) -> None:
            doc["blob"] = "x" * MAX_DOC_BYTES

        with pytest.raises(VaultDocError, match="too large"):
            _doc(vault).update(grow)
        assert vault.attempts == 0

    def test_the_version_is_always_written(self):
        vault = _Vault()

        def drop(doc: dict[str, Any]) -> None:
            doc.pop("version")

        _doc(vault).update(drop)
        assert json.loads(vault.text or "") == {"version": DOC_VERSION}

    def test_an_unreadable_document_is_never_overwritten(self):
        vault = _Vault("{not json")
        with pytest.raises(VaultDocError, match="unreadable"):
            _doc(vault).update(lambda doc: None)
        assert vault.attempts == 0


def test_open_vault_is_this_machines_vault(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    vault = vault_doc.open_vault()
    assert isinstance(vault, SecretVault)
    doc = VaultJsonDoc(vault, NAME)
    doc.update(lambda d: d.update(marker="kept"))
    assert VaultJsonDoc(vault_doc.open_vault(), NAME).read() == {
        "version": DOC_VERSION,
        "marker": "kept",
    }
