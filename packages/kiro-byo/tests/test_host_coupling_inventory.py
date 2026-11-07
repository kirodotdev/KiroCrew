"""Prevent accidental reintroduction of dead Crew adapters and hidden host edits."""
from pathlib import Path
import json

from scripts.audit_crew_host_coupling import inventory, owner_of

ROOT = Path(__file__).resolve().parents[1]


def test_active_host_coupling_inventory_is_consistent():
    result = inventory(ROOT)
    assert result["issues"] == []
    assert result["baseline"] == "0.12.0"
    # Was 20; reduced to 17 after retiring the turn-timing patches
    # (turn_dispatch_gates / turn_stats) and member_sessions.py — see
    # compatibility.json retired_generated_files.
    assert result["mutatedHostFiles"] == 17
    assert result["createdHostFiles"] == 1
    assert result["realHostE2EVerified"] is False
    assert result["externalAppHarnessRegistrationVerified"] is False
    assert result["mutationGroups"]["dashboard"] > 0


def test_all_host_mutations_have_explicit_ownership():
    result = inventory(ROOT)
    assert all(owner_of(path) != "other" for path in result["mutationPaths"])
    assert all(path.startswith("src/kiro_crew/") for path in result["createdPaths"])


def test_inventory_does_not_disguise_a_missing_host_tag_as_certification():
    result = inventory(ROOT)
    assert result["sourceTagValidated"] is False
    assert not result["realHostE2EVerified"]
