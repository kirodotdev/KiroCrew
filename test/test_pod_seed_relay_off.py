"""A seeded pod never hosts on the operator's lease credential.

A set ``tunnel.relay_lease_credential`` starts hosting unless
``tunnel.relay_turned_off`` is set, and the pod seed floor keeps the credential, so
the floor must write the off marker as well as ``relay_enabled=false``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.edition_locks import relay_selected
from kiro_crew.pod import runtime as rt


@pytest.mark.parametrize("credential", ["/abs/cred", "secret://crew-key"])
def test_a_seeded_credential_does_not_start_hosting(
    tmp_path: Path, monkeypatch, credential
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "config.json").write_text(
        json.dumps({"tunnel": {"relay_enabled": True, "relay_lease_credential": credential}}),
        encoding="utf-8",
    )
    out = rt.sanitized_seed_config(seed)
    assert out is not None
    # The credential is kept, as the seed floor promises.
    assert out["tunnel"]["relay_lease_credential"] == credential
    assert out["tunnel"]["relay_turned_off"] is True

    path = tmp_path / "pod" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps(out), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_local_path", lambda: tmp_path / "local.json"
    )
    assert relay_selected(KiroCrewConfig.load()) is False
