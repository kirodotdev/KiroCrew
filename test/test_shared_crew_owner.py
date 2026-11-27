"""The owner's one label (``owner.owner_name``) and its sources; the owner's ids never change."""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

import pytest

from kiro_crew.platform_compat import IS_MACOS
from kiro_crew.shared_crew import access_set, owner
from kiro_crew.shared_crew.contracts import Principal, Provenance
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal


@pytest.fixture(autouse=True)
def _fresh():
    owner.reset_for_tests()
    access_set.reset_for_tests()
    yield
    owner.reset_for_tests()
    access_set.reset_for_tests()


def _scutil(stdout: str = "", returncode: int = 0, calls: list | None = None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    return run


def _raises(exc: BaseException):
    def run(*args, **kwargs):
        raise exc

    return run


def test_the_computer_name_is_read_by_fixed_binary_path():
    calls: list = []
    name = owner._read_computer_name(_scutil("Bolin’s MacBook Air\n", calls=calls))
    assert name == "Bolin’s MacBook Air"
    assert calls == [[owner.SCUTIL_PATH, "--get", "ComputerName"]]
    assert owner.SCUTIL_PATH == "/usr/sbin/scutil"


@pytest.mark.parametrize(
    "run",
    [
        _scutil("", returncode=1),
        _scutil("   \n"),
        _raises(FileNotFoundError()),
        _raises(subprocess.TimeoutExpired("scutil", 2)),
    ],
    ids=["nonzero", "blank", "missing", "timeout"],
)
def test_an_unreadable_computer_name_reads_as_empty_and_never_raises(run):
    assert owner._read_computer_name(run) == ""


def test_the_name_falls_back_to_the_tailnet_host_then_the_os_host_then_the_subject(monkeypatch):
    monkeypatch.setattr(owner, "_read_computer_name", lambda *a, **k: "")
    monkeypatch.setattr(owner.socket, "gethostname", lambda: "os-host")
    if not IS_MACOS:
        assert owner.machine_name(token_subject="local-app") == "os-host"
    owner.set_tailnet_host_name("tailnet-host")
    assert owner.machine_name(token_subject="local-app") == "tailnet-host"
    owner.set_tailnet_host_name("")
    monkeypatch.setattr(owner.socket, "gethostname", lambda: "")
    assert owner.machine_name(token_subject="local-app") == "local-app"


@pytest.mark.skipif(not IS_MACOS, reason="the computer name is read on macOS only")
def test_the_computer_name_wins_on_macos_and_is_read_once(monkeypatch):
    calls: list = []
    monkeypatch.setattr(owner, "_read_computer_name", lambda *a, **k: calls.append(1) or "Studio")
    owner.set_tailnet_host_name("tailnet-host")
    assert owner.machine_name() == "Studio"
    assert owner.machine_name() == "Studio"
    assert calls == [1]


def test_the_owner_subjects_are_labelled_with_the_machine_name_and_keep_their_ids(monkeypatch):
    monkeypatch.setattr(owner, "machine_name", lambda **_kw: "Studio")
    owner.set_owner_subject("owner-sub")
    ids = {}
    for subject in ("local-app", "local-startup", "owner-sub"):
        principal = local_operator_principal(subject)
        assert principal.display.name == "Studio"
        assert principal.subject == subject
        ids[subject] = principal.id
    # The id comes from the subject alone, so a rename never moves anyone.
    monkeypatch.setattr(owner, "machine_name", lambda **_kw: "Renamed")
    assert local_operator_principal("local-app").id == ids["local-app"]
    assert local_operator_principal("local-app").display.name == "Renamed"
    # Any other local token subject keeps its subject as the label.
    assert local_operator_principal("someone-else").display.name == "someone-else"


def test_the_owner_principal_is_built_from_the_configured_owner_subject(monkeypatch):
    monkeypatch.setattr(owner, "machine_name", lambda **_kw: "Studio")
    assert owner.owner_principal().subject == "local-app"
    owner.set_owner_subject("  owner-sub ")
    principal = owner.owner_principal()
    assert principal.subject == "owner-sub"
    assert principal.provenance is Provenance.LOCAL_OPERATOR
    assert principal.display.name == "Studio"


def test_owner_ids_cover_every_local_subject_and_the_owners_tailnet_login():
    owner.set_owner_subject("owner-sub")
    access_set.set_owner("Boss@Example.com", "7")
    ids = owner.owner_principal_ids()
    for subject in ("local-app", "local-startup", "owner-sub"):
        assert local_operator_principal(subject).id in ids
    tailnet = tailnet_principal("boss@example.com", "laptop", Provenance.TAILSCALE_LISTENER)
    assert tailnet.id in ids
    assert owner.is_owner_principal(tailnet) is True
    stranger = tailnet_principal("alice@example.com", "a", Provenance.TAILSCALE_LISTENER)
    assert owner.is_owner_principal(stranger) is False
    assert owner.is_owner_principal(Principal.unknown()) is False
    assert owner.is_owner_principal(Principal.system("internal")) is False


def test_viewer_ids_are_the_owners_for_the_owner_and_empty_for_anyone_else():
    startup = local_operator_principal("local-startup")
    assert owner.owner_principal().id in owner.viewer_ids(startup)
    alice = tailnet_principal("alice@example.com", "a", Provenance.TAILSCALE_LISTENER)
    assert owner.viewer_ids(alice) == frozenset()
    assert owner.viewer_ids(Principal.unknown()) == frozenset()


def test_warming_fills_the_computer_name_cache_off_the_calling_thread(monkeypatch):
    callers: list[str] = []

    def fake_read() -> str:
        callers.append(threading.current_thread().name)
        return "Studio"

    monkeypatch.setattr("kiro_crew.platform_compat.IS_MACOS", True)
    monkeypatch.setattr(owner, "_read_computer_name", fake_read)
    owner.warm_machine_name().join(timeout=5)
    assert callers == ["shared-crew-owner-name"]
    # The cache answers every later read without another lookup.
    assert owner.machine_name() == "Studio"
    assert callers == ["shared-crew-owner-name"]


def test_an_event_loop_thread_never_starts_the_lookup(monkeypatch):
    import asyncio

    calls: list[str] = []

    def fake_read() -> str:
        calls.append("scutil")
        return "Studio"

    monkeypatch.setattr("kiro_crew.platform_compat.IS_MACOS", True)
    monkeypatch.setattr(owner, "_read_computer_name", fake_read)
    owner.set_tailnet_host_name("studio-node")

    async def label() -> str:
        return owner.machine_name()

    # On the loop the fallback answers and the cache stays empty for the warm-up.
    assert asyncio.run(label()) == "studio-node"
    assert calls == []
    assert owner.machine_name() == "Studio"
    assert calls == ["scutil"]


# -- owner_name: the one label, first non-empty source wins ------------------ #


def test_owner_name_falls_through_email_then_tailscale_then_the_machine(monkeypatch):
    monkeypatch.setattr(owner, "machine_name", lambda **_kw: "Studio")
    # 4. Nothing else known: this machine's name.
    assert owner.owner_name() == "Studio"
    # 3. The owner's Tailscale login, then their Tailscale display name over it.
    access_set.set_owner("boss@example.com", "7")
    assert owner.owner_name() == "boss@example.com"
    owner.set_tailnet_display_name("Bo Boss")
    assert owner.owner_name() == "Bo Boss"
    # 2. The signed-in Kiro account's e-mail wins over every Tailscale name.
    owner.set_login_email("bo@kiro.example")
    assert owner.owner_name() == "bo@kiro.example"
    # A reading that names no account drops back to the next source.
    owner.set_login_email(None)
    assert owner.owner_name() == "Bo Boss"


@pytest.mark.parametrize("raw", ["", "  ", "not-an-email", "a@b", 42, None, "x\n@y.example"])
def test_only_an_email_is_kept_as_the_login_label(raw):
    owner.set_login_email(raw)
    assert owner.login_email() == ""


def test_the_owner_principal_and_its_local_subjects_carry_owner_name(monkeypatch):
    monkeypatch.setattr(owner, "machine_name", lambda **_kw: "Studio")
    owner.set_login_email(" bo@kiro.example ")
    assert owner.owner_principal().display.name == "bo@kiro.example"
    assert local_operator_principal("local-startup").display.name == "bo@kiro.example"
    # The label never moves an id.
    assert local_operator_principal("local-startup").id in owner.owner_principal_ids()


@pytest.mark.asyncio
async def test_warming_reads_the_signed_in_identity_once(monkeypatch):
    from kiro_crew.dashboard.handlers import sessions

    async def identity():
        return {"email": "bo@kiro.example", "account_type": "IdC"}

    monkeypatch.setattr(sessions, "fetch_local_identity", identity)
    await owner.warm_login_email()
    assert owner.login_email() == "bo@kiro.example"


@pytest.mark.asyncio
async def test_warming_keeps_a_usage_reading_that_landed_first(monkeypatch):
    from kiro_crew.dashboard.handlers import sessions

    async def identity():
        return {"email": "old@kiro.example"}

    monkeypatch.setattr(sessions, "fetch_local_identity", identity)
    owner.set_login_email("new@kiro.example")
    await owner.warm_login_email()
    assert owner.login_email() == "new@kiro.example"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, RuntimeError("whoami broke")], ids=["unknown", "raises"])
async def test_warming_an_unknown_identity_leaves_the_label_to_the_next_source(monkeypatch, answer):
    from kiro_crew.dashboard.handlers import sessions

    async def identity():
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(sessions, "fetch_local_identity", identity)
    await owner.warm_login_email()
    assert owner.login_email() == ""
    # Still unset, so a later reading or warm-up may fill it.
    owner.set_login_email("bo@kiro.example")
    assert owner.login_email() == "bo@kiro.example"


def test_every_published_usage_reading_moves_the_label_with_the_account(monkeypatch):
    from kiro_crew.dashboard.handlers import sessions

    # The published cache is module state: put it back after the test.
    for name in ("_usage_cache", "_usage_cache_ts", "_usage_cache_vault_fp"):
        monkeypatch.setattr(sessions, name, getattr(sessions, name))
    monkeypatch.setattr(sessions, "_usage_cache_user_id", sessions._usage_cache_user_id)
    monkeypatch.setattr(sessions, "_vault_signin_marker", sessions._vault_signin_marker)
    sessions._publish_usage({"credits_plan": 1000, "email": "a@kiro.example"})
    assert owner.login_email() == "a@kiro.example"
    sessions._publish_usage({"credits_plan": 1000, "email": "b@kiro.example"})
    assert owner.login_email() == "b@kiro.example"
    # An unavailable marker names no account.
    sessions._publish_usage({"available": False})
    assert owner.login_email() == ""


def test_the_status_self_user_display_name_is_read_for_the_owner():
    from kiro_crew.dashboard.tailnet_direct import parse_status

    status = {
        "BackendState": "Running",
        "Self": {"TailscaleIPs": ["100.64.0.1"], "UserID": 11, "HostName": "studio"},
        "User": {"11": {"LoginName": "boss@example.com", "DisplayName": "Bo Boss"}},
    }
    node = parse_status(status)
    assert node is not None and node.display_name == "Bo Boss"
    status["User"]["11"].pop("DisplayName")
    assert parse_status(status).display_name == ""


def test_learning_the_owner_from_the_daemon_records_their_tailscale_name(monkeypatch):
    from kiro_crew.dashboard import tailnet_direct_listener as listener
    from kiro_crew.dashboard.tailnet_direct import SelfNode

    monkeypatch.setattr(owner, "machine_name", lambda **_kw: "Studio")
    monkeypatch.setattr(access_set, "set_owner", lambda *_a: None)
    node = SelfNode(addresses=(), login="boss@example.com", user_id="7", display_name="Bo Boss")
    listener._publish_owner(node)
    assert owner.owner_name() == "Bo Boss"
