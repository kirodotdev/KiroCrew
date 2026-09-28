"""Tests for the deploy_web profile control plane (profiles.py + handler endpoints)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.deploy import handlers
from kiro_crew.deploy import profiles as profiles_mod


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(profiles_mod, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: tmp_path / "profiles.json")
    monkeypatch.setattr(profiles_mod, "_legacy_registry_path", lambda: tmp_path / "legacy_profiles.json")
    monkeypatch.setattr(profiles_mod, "_legacy_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(handlers, "_data_dir", lambda: tmp_path)
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


class _FakeReq:
    def __init__(self, body=None, name=""):
        self._body = body or {}
        self.match_info = {"name": name}
        # The fail-closed _deny_restricted guard requires a real (non-None)
        # dashboard state on the request app and readable headers; an empty
        # X-Session-Key means "not a restricted session".
        self.app = {"state": object()}
        self.headers = {}

    async def json(self):
        return self._body


def _register(name="p1", region="us-west-2", default=False):
    reg = profiles_mod.load_registry()
    reg["profiles"].append(profiles_mod.make_entry(name, region))
    if default or not reg["default"]:
        reg["default"] = name
    profiles_mod.save_registry(reg)


# --- registry / migration ---------------------------------------------------

def test_v1_config_migrates_to_registry(tmp_path: Path):
    (tmp_path / "config.json").write_text(
        json.dumps({"profile": "legacy-p", "region": "eu-west-1"}))
    reg = profiles_mod.load_registry()
    assert reg["default"] == "legacy-p"
    assert reg["profiles"][0]["region"] == "eu-west-1"


def test_resolve_default_named_and_unknown():
    _register("a", "us-west-2", default=True)
    _register("b", "us-east-1")
    assert profiles_mod.resolve_profile("") == ("a", "us-west-2")
    assert profiles_mod.resolve_profile("b") == ("b", "us-east-1")
    assert profiles_mod.resolve_profile("ghost") is None


def test_legacy_config_shim_upserts_registry_default():
    handlers._save_config("shim-p", "ap-southeast-2")
    reg = profiles_mod.load_registry()
    assert reg["default"] == "shim-p"
    assert handlers._load_config() == {"profile": "shim-p", "region": "ap-southeast-2"}


# --- endpoints ----------------------------------------------------------------

def test_profiles_post_registers_and_defaults(monkeypatch):
    monkeypatch.setattr(profiles_mod, "discover_aws_profiles", lambda: [])
    audits: list[tuple[str, str, str]] = []
    monkeypatch.setattr(handlers, "_audit",
                        lambda action, sid, outcome, **kw: audits.append((action, sid, outcome)))
    resp = _run(handlers._handle_profiles_post(_FakeReq({"name": "p1", "region": "us-west-2"})))
    assert resp.status == 200
    reg = profiles_mod.load_registry()
    assert reg["default"] == "p1"  # first registration becomes default
    # Registration is audited even WITHOUT create=true (the name becomes deployable).
    assert ("profile_register", "p1", "allowed") in audits


def test_region_spec_accepts_govcloud():
    from kiro_crew.validation import validate_field
    assert validate_field("us-gov-west-1", profiles_mod.REGION_SPEC) == "us-gov-west-1"
    assert validate_field("ap-southeast-2", profiles_mod.REGION_SPEC) == "ap-southeast-2"
    with pytest.raises(Exception):
        validate_field("not_a_region", profiles_mod.REGION_SPEC)


def test_profiles_post_rejects_bad_name():
    resp = _run(handlers._handle_profiles_post(_FakeReq({"name": "evil;rm -rf", "region": "us-west-2"})))
    assert resp.status == 400


def test_profiles_delete_registry_only_and_default_reassign(tmp_path: Path, monkeypatch):
    audits: list[tuple[str, str, str]] = []
    monkeypatch.setattr(handlers, "_audit",
                        lambda action, sid, outcome, **kw: audits.append((action, sid, outcome)))
    _register("a", default=True)
    _register("b")
    resp = _run(handlers._handle_profiles_delete(_FakeReq(name="a")))
    assert resp.status == 200
    reg = profiles_mod.load_registry()
    assert [p["name"] for p in reg["profiles"]] == ["b"]
    assert reg["default"] == "b"  # default reassigned, not left dangling
    # Removing a deployable identity is a permission decision → SEL-audited.
    assert ("profile_delete", "a", "allowed") in audits


def test_profiles_put_sets_default_and_region(monkeypatch):
    audits: list[tuple[str, str, str]] = []
    monkeypatch.setattr(handlers, "_audit",
                        lambda action, sid, outcome, **kw: audits.append((action, sid, outcome)))
    _register("a", default=True)
    _register("b")
    resp = _run(handlers._handle_profiles_put(_FakeReq({"default": True, "region": "eu-west-1"}, name="b")))
    assert resp.status == 200
    reg = profiles_mod.load_registry()
    assert reg["default"] == "b"
    assert profiles_mod.get_entry(reg, "b")["region"] == "eu-west-1"
    # Changing the default identity is a permission decision → SEL-audited.
    assert ("profile_update", "b", "allowed") in audits


def test_deploy_rejects_unregistered_profile():
    _register("real", default=True)
    # _resolve_profile is async (registry IO offloaded via asyncio.to_thread).
    with pytest.raises(handlers._ProfileResolveError) as exc:
        _run(handlers._resolve_profile({"profile": "ghost"}))
    assert "not registered" in exc.value.payload["error"]


def test_deploy_resolves_dropdown_choice_over_default():
    _register("dflt", "us-west-2", default=True)
    _register("picked", "us-east-1")
    assert _run(handlers._resolve_profile({"profile": "picked"})) == ("picked", "us-east-1")
    assert _run(handlers._resolve_profile({})) == ("dflt", "us-west-2")


def test_profiles_put_delete_reject_invalid_name():
    _register("real", default=True)
    for handler in (handlers._handle_profiles_put, handlers._handle_profiles_delete):
        resp = _run(handler(_FakeReq({}, name="evil;rm -rf")))
        assert resp.status == 400


def test_verify_backfill_audited(monkeypatch):
    audits: list[tuple[str, str, str]] = []
    monkeypatch.setattr(handlers, "_audit",
                        lambda action, sid, outcome, **kw: audits.append((action, sid, outcome)))
    _register("p", default=True)
    monkeypatch.setattr(handlers.iam_mod, "reachability_check",
                        lambda profile: {"reachable": True, "account": "123456789012"})
    resp = _run(handlers._handle_verify(_FakeReq({"profile": "p"})))
    assert resp.status == 200
    reg = profiles_mod.load_registry()
    assert profiles_mod.get_entry(reg, "p")["account"] == "123456789012"
    assert ("profile_verify", "p", "allowed") in audits


# --- write path security boundary --------------------------------------------

def test_configure_set_refuses_credential_keys():
    # Defense in depth: credential-material keys can never be written even if a
    # future caller passes them.
    assert profiles_mod._configure_set("aws_access_key_id", "AKIA...", "p") is not None
    assert profiles_mod._configure_set("aws_secret_access_key", "x", "p") is not None


def test_create_aws_profile_validates_all_inputs():
    assert "invalid" in profiles_mod.create_aws_profile("bad name!", "us-west-2")
    assert "invalid" in profiles_mod.create_aws_profile("ok", "US-WEST-2")
    assert "invalid" in profiles_mod.create_aws_profile("ok", "us-west-2", account="123", role="Admin")
    assert "invalid" in profiles_mod.create_aws_profile("ok", "us-west-2", account="123456789012", role="rm -rf /")


def test_create_aws_profile_writes_only_allowlisted_keys(monkeypatch, tmp_path):
    written: list[tuple[str, str, str]] = []

    def fake_run_aws(args, profile, timeout=30):
        if args[:2] == ["configure", "set"]:
            written.append((args[2], args[3], profile))
        return 0, "", ""

    monkeypatch.setattr(profiles_mod.engine, "run_aws", fake_run_aws)
    err = profiles_mod.create_aws_profile(
        "iso-p", "us-west-2", account="123456789012", role="Admin")
    assert err is None
    keys = [k for k, _v, _p in written]
    assert keys == ["region", "credential_process"]
    cred = next(v for k, v, _p in written if k == "credential_process")
    # The generated command must satisfy the AWS credential_process contract:
    # shlex-parseable (botocore splits it), targets the requested role, and
    # when executed emits JSON with "Version": 1. Execute it for real against
    # a fake `aws` on PATH -- string-matching quoting is how the previous
    # (broken) nested-bash version slipped through.
    import json
    import shlex
    import subprocess

    assert "arn:aws:iam::123456789012:role/Admin" in cred
    parts = shlex.split(cred)
    assert parts[:2] == [sys.executable, "-c"]
    assert len(parts) == 3
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_aws = fakebin / "aws"
    fake_aws.write_text(
        "#!/usr/bin/env python3\nimport json\n"
        'print(json.dumps({"Credentials": {"AccessKeyId": "AKIATEST",'
        ' "SecretAccessKey": "sk", "SessionToken": "tok",'
        ' "Expiration": "2026-01-01T00:00:00Z"}}))\n'
    )
    fake_aws.chmod(0o755)
    env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}")
    proc = subprocess.run(parts, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out == {
        "Version": 1,
        "AccessKeyId": "AKIATEST",
        "SecretAccessKey": "sk",
        "SessionToken": "tok",
        "Expiration": "2026-01-01T00:00:00Z",
    }


def test_list_merges_profiles_and_dedupes(monkeypatch):
    _register("a", default=True)
    _register("b")

    def fake_list_sites(profile, region):
        if profile == "a":
            return [{"site_id": "s1", "distribution_id": "D1", "bucket": "b1"}]
        return [{"site_id": "s1", "distribution_id": "D1", "bucket": "b1"},  # same account → dedupe
                {"site_id": "s2", "distribution_id": "D2", "bucket": "b2"}]

    async def run():
        return await handlers._do_list()

    monkeypatch.setattr(handlers.engine, "list_sites", fake_list_sites)
    status, payload = _run(run())
    assert status == 200
    got = {(s["site_id"], s["profile"]) for s in payload["sites"]}
    assert got == {("s1", "a"), ("s2", "b")}  # D1 attributed once, to profile a


# --- B: capacity check ordering (finding B) -----------------------------------

def test_capacity_check_before_create_side_effect(monkeypatch):
    """Capacity 400 must fire BEFORE create_aws_profile touches ~/.aws/config."""
    # Fill registry to 50
    for i in range(50):
        _register(f"p{i}")
    created = []
    monkeypatch.setattr(profiles_mod, "create_aws_profile",
                        lambda *a, **kw: (created.append(1), "")[1])  # no error, but track call
    monkeypatch.setattr(handlers, "_audit", lambda *a, **kw: None)
    resp = _run(handlers._handle_profiles_post(
        _FakeReq({"name": "overflow", "region": "us-west-2", "create": True})))
    assert resp.status == 400
    assert "full" in json.loads(resp.body)["error"]
    # Critical: create_aws_profile must NOT have been called
    assert created == []


def test_profile_create_error_redacted_in_response(monkeypatch):
    """Raw aws-CLI stderr from create_aws_profile must be redacted before it
    reaches the dashboard response (the SEL audit keeps the raw text)."""
    fake_key = "AKIAIOSFODNN7EXAMPLE"
    monkeypatch.setattr(handlers.profiles_mod, "create_aws_profile",
                        lambda *a, **k: f"aws configure failed: key {fake_key} rejected")
    resp = _run(handlers._handle_profiles_post(
        _FakeReq({"name": "newprof", "region": "us-west-2", "create": True})))
    assert resp.status == 400
    assert fake_key not in resp.text
    assert "error" in resp.text


def test_locked_registry_concurrent_writes_survive():
    """Two concurrent mutations under locked_registry don't lose each other."""
    import concurrent.futures

    # Seed one profile
    profiles_mod.save_registry({"version": 2, "profiles": [
        profiles_mod.make_entry("alpha", "us-west-2"),
    ], "default": "alpha"})

    def add_beta():
        with profiles_mod.locked_registry() as reg:
            reg["profiles"].append(profiles_mod.make_entry("beta", "us-east-1"))

    def add_gamma():
        with profiles_mod.locked_registry() as reg:
            reg["profiles"].append(profiles_mod.make_entry("gamma", "eu-west-1"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(add_beta)
        f2 = ex.submit(add_gamma)
        f1.result()
        f2.result()

    final = profiles_mod.load_registry()
    names = sorted(p["name"] for p in final["profiles"])
    assert names == ["alpha", "beta", "gamma"], f"Expected all three, got {names}"


# --- profile redaction in GET response (item 1 R18) --------------------------

def test_profiles_get_redacts_credentials_in_note(monkeypatch):
    """A note containing a fake AKIA key is redacted in the GET /profiles response."""
    # GET also discovers CLI profiles via subprocess — mock it out (sandbox-free env).
    monkeypatch.setattr(profiles_mod, "discover_aws_profiles", lambda: [])
    # Register a profile with a credential-like value in the note field
    fake_akia = "AKIAIOSFODNN7EXAMPLE"
    profiles_mod.save_registry({
        "version": 2,
        "profiles": [
            {**profiles_mod.make_entry("cred-test", "us-west-2"), "note": f"key: {fake_akia}"},
        ],
        "default": "cred-test",
    })
    resp = _run(handlers._handle_profiles_get(_FakeReq()))
    import json as _json
    body = _json.loads(resp.text)
    # The raw AKIA key must NOT appear in the response
    assert fake_akia not in resp.text
    # But the profile entry should still be there
    assert len(body["profiles"]) == 1
    assert body["profiles"][0]["name"] == "cred-test"
