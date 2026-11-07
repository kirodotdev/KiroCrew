"""``PersonalDriveProvider.source_problem``: why the drive copy cannot be read.

A publication is bound to the AWS profile it was published under. When that
profile is not in the registry, every call for the artifact refuses, and the
artifact page says why instead of showing a plain sync banner. The check reads
the local profile registry only, so opening an artifact page makes no AWS call.
"""

from __future__ import annotations

import asyncio

import pytest
from publish_personal_drive_helpers import _clean_registry  # noqa: F401 - fixture
from publish_personal_drive_helpers import ACCOUNT_ID, _stub_profiles

from kiro_crew.publish import personal_drive as pd

KEY = "a" * 32


@pytest.fixture(autouse=True)
def _no_aws(_floor_monkeypatch):
    def _refuse(*args, **kwargs):
        raise AssertionError("source_problem must not call AWS")

    _floor_monkeypatch.setattr(pd.engine, "run_aws", _refuse)
    _floor_monkeypatch.setattr(pd.engine, "_checked", _refuse)


def _problem(external_id: str):
    return asyncio.run(pd.PersonalDriveProvider().source_problem(external_id=external_id))


def test_an_unregistered_bound_profile_says_why(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha"], default="alpha")
    text, code = _problem(f"{KEY}~{ACCOUNT_ID}~gone-profile")
    assert code == "personal_drive_profile_not_registered"
    assert "gone-profile" in text
    assert "local copy is unchanged" in text


def test_a_registered_bound_profile_has_no_problem(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha", "beta"], default="alpha")
    assert _problem(f"{KEY}~{ACCOUNT_ID}~beta") is None


def test_an_unbound_handle_has_no_problem(monkeypatch):
    # A handle minted before binding follows the registry default.
    _stub_profiles(monkeypatch, ["alpha"], default="alpha")
    assert _problem(KEY) is None
