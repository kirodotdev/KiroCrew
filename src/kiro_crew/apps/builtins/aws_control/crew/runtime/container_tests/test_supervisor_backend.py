"""Tests for ``container.supervisor.backend`` -- launch argv/env and readiness.

Readiness is exercised against a fake backend process that binds a real
loopback port and writes a real secret file. No Kiro Crew gateway is booted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from container import common
from container.common import Settings
from container.supervisor import backend as backend_mod
from container.supervisor.backend import (
    BackendExited,
    BackendReadyTimeout,
    build_backend_argv,
    build_backend_env,
    require_model_identity,
    seed_model_identity,
    start_backend,
    wait_until_ready,
)

from .test_supervisor_fakes import fake_argv, free_port, write_fake_backend


def make_settings(tmp_path: Path, port: int) -> Settings:
    data_home = tmp_path / "data"
    return Settings(
        backend_port=port,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=data_home,
        config_dir=data_home / "config",
        crew_name="test-crew",
        backup_bucket=None,
        backup_prefix="",
    )


# --- argv ------------------------------------------------------------------


def test_argv_runs_dashboard_mode_never_no_dashboard(tmp_path):
    argv = build_backend_argv(make_settings(tmp_path, 8765))
    # Dashboard mode is required. --slack-only is the real flag that removes it
    # (cli.py:539); --no-dashboard is not even a real flag. Pass NEITHER.
    assert backend_mod.FLAG_SLACK_ONLY not in argv
    assert backend_mod.FLAG_NO_DASHBOARD not in argv
    assert backend_mod.GATEWAY_SUBCOMMAND in argv


def test_argv_arms_no_crons(tmp_path):
    # Arming the scheduler fires overdue jobs immediately on boot.
    assert backend_mod.FLAG_NO_CRONS in build_backend_argv(make_settings(tmp_path, 8765))


def test_argv_sets_approval_yolo(tmp_path):
    # Unattended crew: no one is there to answer a tool-approval prompt.
    argv = build_backend_argv(make_settings(tmp_path, 8765))
    assert backend_mod.FLAG_APPROVAL in argv
    i = argv.index(backend_mod.FLAG_APPROVAL)
    assert argv[i + 1] == backend_mod.APPROVAL_MODE == "yolo"


# --- credential ------------------------------------------------------------


def identity_json(**overrides) -> str:
    """One delivered model identity, in the shape Secrets Manager carries.

    ``expires_at`` is far in the future so ``KasToken.is_usable`` is true on the
    access token alone: the delivery path is what these tests measure, and a
    fixture that leaned on ``refresh_token`` would pass even if the expiry were
    dropped on the way into the vault.

    ``profile_arn`` is always present because ``TokenStore.load`` drops a ``social``
    or ``identity_center`` entry without one -- so a fixture omitting it would write
    those slots and then read back as if nothing had been stored, which would make a
    test about slot cleanup pass for the wrong reason.
    """
    doc = {
        "access_token": "atk-test",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "provider": "BuilderId",
        "identity": "builder_id",
        "profile_arn": "arn:aws:profile/test",
    }
    doc.update(overrides)
    return json.dumps(doc)


def test_env_withholds_the_model_credential_in_both_shapes(tmp_path):
    """The half of the interlock that makes an unsandboxed posture safe.

    The backend spawns the model worker, that worker auto-approves every tool it
    calls, and its prompt content is untrusted -- so a credential readable there is
    reachable by prompt content whatever the sandbox is doing. Both shapes are
    checked because covering one and not its sibling leaves the same path open.
    """
    env = build_backend_env(
        make_settings(tmp_path, 9001),
        base={"KIRO_IDENTITY": identity_json(), "KIRO_API_KEY": "sk-test"},
    )
    assert backend_mod.ENV_KIRO_IDENTITY not in env
    assert backend_mod.ENV_KIRO_API_KEY not in env


def test_seed_writes_the_delivered_identity_where_the_backend_will_read_it(tmp_path):
    """Seeding the vault is what lets the relay spawn host-owned rather than cli-owned."""
    s = make_settings(tmp_path, 9001)
    assert seed_model_identity(s, source={"KIRO_IDENTITY": identity_json()}) is True
    require_model_identity(s)  # must not raise


def test_seeding_makes_the_delivered_identity_the_RESOLVED_one(tmp_path):
    """A stale higher-ranked slot from a prior task must not win.

    ``resolve`` returns the highest-ranked slot present rather than the newest write,
    and ``save`` touches only its own slot. The data home is a persistent volume that
    carries over between tasks, so a slot a prior task left behind survives -- and it
    stays usable while it holds a refresh token. Delivering an identity of a LOWER rank
    than that leftover is an ordinary migration, and without cleanup the task would
    authenticate as the previous account with nothing disagreeing: every reader
    downstream calls this same resolver.
    """
    from kiro_crew.auth.store import KasToken, TokenStore

    s = make_settings(tmp_path, 9001)
    store = TokenStore(s.data_home)
    # A prior task's identity, one rung ABOVE the one about to be delivered.
    store.save(KasToken.from_json(identity_json(identity="external_idp", provider="ExternalIdp")))
    stale = store.resolve()
    assert stale is not None and stale.identity == "external_idp", "precondition: stale slot wins"

    seed_model_identity(s, source={"KIRO_IDENTITY": identity_json(identity="builder_id")})

    resolved = TokenStore(s.data_home).resolve()
    assert resolved is not None
    assert resolved.identity == "builder_id", "the delivered identity must be the resolved one"
    assert resolved.access_token == "atk-test"


def test_every_other_slot_is_emptied_not_just_the_higher_ranked_ones(tmp_path):
    """Lower-ranked leftovers go too, so the vault holds exactly what was delivered.

    Leaving a lower-ranked slot would not change today's `resolve`, but it leaves a
    live credential for another account in a store this task owns -- and it would start
    deciding the moment the delivered identity's own slot lapsed.
    """
    from kiro_crew.auth.store import KNOWN_IDENTITIES, KasToken, TokenStore

    s = make_settings(tmp_path, 9001)
    store = TokenStore(s.data_home)
    for kind in KNOWN_IDENTITIES:
        store.save(KasToken.from_json(identity_json(identity=kind)))

    seed_model_identity(s, source={"KIRO_IDENTITY": identity_json(identity="identity_center")})

    survivors = sorted(k for k in KNOWN_IDENTITIES if TokenStore(s.data_home).load(k) is not None)
    assert survivors == ["identity_center"], survivors


def test_an_unusable_delivery_refuses_without_destroying_a_valid_identity(tmp_path):
    """The ordering that makes a bad delivery non-destructive.

    ``from_json`` validates shape and nothing else, so a token that has expired with
    nothing to renew it still parses. On a persistent volume the vault can hold a token
    that refreshed past the Secrets Manager copy, which makes that copy the OLDER
    credential -- so writing it and emptying the other slots before discovering it is
    dead would leave no live credential anywhere and only a human sign-in to recover.
    Validating first means the refusal costs nothing.
    """
    from kiro_crew.auth.store import KasToken, TokenStore

    s = make_settings(tmp_path, 9001)
    store = TokenStore(s.data_home)
    store.save(KasToken.from_json(identity_json(identity="external_idp", provider="ExternalIdp")))

    dead = identity_json(
        identity="builder_id", expires_at="2000-01-01T00:00:00+00:00", refresh_token=None
    )
    with pytest.raises(common.ConfigError, match="cannot produce an access token"):
        seed_model_identity(s, source={"KIRO_IDENTITY": dead})

    # The credential that was already there is still there, and still resolves.
    survivor = TokenStore(s.data_home).resolve()
    assert survivor is not None, "the refusal destroyed the vault it was protecting"
    assert survivor.identity == "external_idp"
    assert TokenStore(s.data_home).load("builder_id") is None, "the dead token was written"


def test_a_delivery_the_store_will_not_keep_refuses_before_overwriting_its_own_slot(tmp_path):
    """The SAME slot, which is where the loss happens.

    ``save`` replaces the delivered identity's own entry, so checking acceptance
    afterwards is too late however carefully the other slots are handled: the credential
    that was in that slot is already gone, and the refusal that follows leaves nothing but
    a human sign-in to recover it. A ``social`` token with no ``profile_arn`` is the
    ordinary way to arrive here -- ``KasToken``'s docstring calls the field optional for
    social identities while ``load`` requires it.

    The survivor is deliberately in the same slot as the delivery. An earlier version of
    this test put it in a different one, which passed while proving only that the OTHER
    slots survive.
    """
    from kiro_crew.auth.store import KasToken, TokenStore

    s = make_settings(tmp_path, 9001)
    store = TokenStore(s.data_home)
    live = identity_json(
        identity="social",
        provider="Google",
        profile_arn="arn:aws:profile/live",
        access_token="keep",
    )
    store.save(KasToken.from_json(live))

    no_arn = identity_json(identity="social", provider="Google", profile_arn=None)
    with pytest.raises(common.ConfigError, match="no.*profile ARN"):
        seed_model_identity(s, source={"KIRO_IDENTITY": no_arn})

    survivor = TokenStore(s.data_home).load("social")
    assert survivor is not None, "the refusal destroyed the credential in the slot it wrote"
    assert survivor.access_token == "keep"


def test_an_expired_delivery_that_cannot_refresh_refuses_before_overwriting_its_slot(tmp_path):
    """Expired WITH a refresh token is still dead when the refresh cannot be attempted.

    ``is_usable`` accepts an expired token on the strength of a refresh token alone,
    because the three host-side readers of that predicate want the looser question. A
    ``builder_id`` refresh also needs stored client credentials -- ``refresh.py`` refuses
    without them before any network call -- so a delivery carrying a refresh token and no
    client credentials passes ``is_usable`` and then fails every single turn.

    The survivor is in the same slot as the delivery, which is where the loss happens.
    """
    from kiro_crew.auth.store import KasToken, TokenStore

    s = make_settings(tmp_path, 9001)
    store = TokenStore(s.data_home)
    live = identity_json(
        identity="builder_id", access_token="keep", client_id="c", client_secret="x"
    )
    store.save(KasToken.from_json(live))

    stale = identity_json(
        identity="builder_id",
        expires_at="2000-01-01T00:00:00+00:00",
        refresh_token="present-but-unusable",
        client_id=None,
        client_secret=None,
    )
    with pytest.raises(common.ConfigError, match="cannot be renewed"):
        seed_model_identity(s, source={"KIRO_IDENTITY": stale})

    survivor = TokenStore(s.data_home).load("builder_id")
    assert survivor is not None, "the refusal destroyed the credential in the slot it wrote"
    assert survivor.access_token == "keep"


def test_an_expired_delivery_that_can_refresh_is_still_accepted(tmp_path):
    """The complement, so the new refusal cannot be a blanket ban on expired tokens.

    An expired ``builder_id`` token WITH its client credentials is renewable, which is the
    ordinary state of a credential provisioned some hours ago. It must still seed.
    """
    from kiro_crew.auth.store import TokenStore

    s = make_settings(tmp_path, 9001)
    renewable = identity_json(
        identity="builder_id",
        expires_at="2000-01-01T00:00:00+00:00",
        refresh_token="good",
        client_id="c",
        client_secret="x",
    )
    assert seed_model_identity(s, source={"KIRO_IDENTITY": renewable}) is True

    seeded = TokenStore(s.data_home).load("builder_id")
    assert seeded is not None and seeded.refresh_token == "good"


def test_a_delivery_the_store_will_not_keep_leaves_the_other_slots_alone_too(tmp_path):
    """The complementary half: the sweep must not have run either."""
    from kiro_crew.auth.store import KasToken, TokenStore

    s = make_settings(tmp_path, 9001)
    store = TokenStore(s.data_home)
    store.save(KasToken.from_json(identity_json(identity="builder_id")))

    no_arn = identity_json(identity="identity_center", provider="Enterprise", profile_arn=None)
    with pytest.raises(common.ConfigError, match="no.*profile ARN"):
        seed_model_identity(s, source={"KIRO_IDENTITY": no_arn})

    survivor = TokenStore(s.data_home).resolve()
    assert survivor is not None and survivor.identity == "builder_id"


def test_seed_reports_false_when_nothing_was_delivered(tmp_path):
    """False here is a signal the caller must act on, not an outcome.

    ``run()`` turns it into a refusal; a test that it is reported at all belongs with
    the function, and the test that it is FATAL belongs with ``run()``.
    """
    s = make_settings(tmp_path, 9001)
    assert seed_model_identity(s, source={}) is False
    assert seed_model_identity(s, source={"KIRO_IDENTITY": "   "}) is False


def test_nothing_is_emptied_when_nothing_was_delivered(tmp_path):
    """A task that delivers no identity must not wipe the vault it inherited.

    The cleanup belongs to a successful seed. Running it on the no-delivery path would
    turn a misconfigured task into a destructive one.
    """
    from kiro_crew.auth.store import KasToken, TokenStore

    s = make_settings(tmp_path, 9001)
    TokenStore(s.data_home).save(KasToken.from_json(identity_json(identity="builder_id")))
    assert seed_model_identity(s, source={}) is False
    survivor = TokenStore(s.data_home).resolve()
    assert survivor is not None and survivor.identity == "builder_id"


def test_require_model_identity_raises_when_the_vault_holds_nothing(tmp_path):
    s = make_settings(tmp_path, 9001)
    with pytest.raises(common.ConfigError, match="KIRO_IDENTITY"):
        require_model_identity(s)


def test_seed_refuses_a_value_that_is_not_an_identity_document(tmp_path):
    """An unparseable delivery would boot a backend whose every turn fails."""
    s = make_settings(tmp_path, 9001)
    with pytest.raises(common.ConfigError, match="not a model identity document"):
        seed_model_identity(s, source={"KIRO_IDENTITY": "sk-an-api-key-not-a-token"})


def test_seed_refuses_an_identity_kind_the_vault_cannot_store(tmp_path):
    """``TokenStore.save`` raises ``ValueError`` for an unknown kind; it must not escape raw.

    The refusal names that no other slot was touched, which is the property the ordering
    buys: a delivery the store cannot even accept costs the vault nothing.
    """
    s = make_settings(tmp_path, 9001)
    with pytest.raises(common.ConfigError, match="no other slot has been touched"):
        seed_model_identity(s, source={"KIRO_IDENTITY": identity_json(identity="nonesuch")})


def test_the_vault_read_is_addressed_by_the_crews_data_home(tmp_path, monkeypatch):
    """Not by ``KIROCREW_HOME``, which the SUPERVISOR's environment does not carry.

    ``build_backend_env`` sets that variable for the BACKEND, so a check resolving
    its store the way the gateway does would read whichever home the supervisor
    happened to inherit -- passing on a machine whose ambient home has an identity
    and failing on one whose does not, in both cases without consulting the vault the
    backend will actually read. Pointing the ambient home at an empty directory while
    the crew's own vault is seeded is what tells the two apart.
    """
    s = make_settings(tmp_path, 9001)
    seed_model_identity(s, source={"KIRO_IDENTITY": identity_json()})
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "somewhere-else"))
    require_model_identity(s)  # must not raise: it reads s.data_home, not the env


# --- env -------------------------------------------------------------------


def test_env_points_at_shared_home_and_loopback_port(tmp_path):
    s = make_settings(tmp_path, 9001)
    env = build_backend_env(s, base={})
    assert env[backend_mod.ENV_HOME] == str(s.data_home)
    assert env[backend_mod.ENV_PORT] == "9001"
    # The bind address is pinned to loopback (dashboard/urls.py:208 reads this).
    assert env[backend_mod.ENV_BIND] == common.BACKEND_HOST == "127.0.0.1"


def test_env_pins_loopback_over_an_inherited_bind_all(tmp_path):
    # The official image sets KIROCREW_BIND=0.0.0.0; the backend must never be
    # network-reachable, so our env must override that back to loopback.
    env = build_backend_env(make_settings(tmp_path, 9001), base={"KIROCREW_BIND": "0.0.0.0"})
    assert env[backend_mod.ENV_BIND] == "127.0.0.1"


def test_env_disables_the_beacon(tmp_path):
    # KIROCREW_TELEMETRY_DISABLED (truthy) is the opt-out beacon.py actually reads.
    env = build_backend_env(make_settings(tmp_path, 9001), base={})
    assert env[backend_mod.ENV_TELEMETRY_DISABLED].strip().lower() in {"1", "true", "yes", "on"}


def test_env_strips_leaked_channel_credentials(tmp_path):
    base = {
        "TELEGRAM_BOT_TOKEN": "leaked",
        "SLACK_BOT_TOKEN": "leaked",
        "KEEP_ME": "yes",
    }
    env = build_backend_env(make_settings(tmp_path, 9001), base=base)
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "SLACK_BOT_TOKEN" not in env
    assert env["KEEP_ME"] == "yes"  # unrelated env is preserved


# --- readiness -------------------------------------------------------------


def test_wait_until_ready_returns_when_port_and_secret_are_both_up(tmp_path):
    port = free_port()
    s = make_settings(tmp_path, port)
    script = write_fake_backend(tmp_path)
    pg = start_backend(
        s,
        argv=fake_argv(
            script,
            port=port,
            run_dir=str(s.backend_run_dir),
            secret_delay=0.4,
            ttl=30,
        ),
    )
    try:
        wait_until_ready(s, timeout=10.0, process=pg, poll_interval=0.05)
        # The secret the fake wrote is exactly what common reads back.
        assert common.read_boot_secret(s.backend_run_dir, port) == "fake-boot-secret"
    finally:
        pg.terminate(2.0)


def test_wait_until_ready_times_out_when_secret_present_but_port_closed(tmp_path):
    # A present secret alone is NOT ready. Pre-write the secret, start nothing.
    port = free_port()
    s = make_settings(tmp_path, port)
    s.backend_run_dir.mkdir(parents=True)
    common.secret_path(s.backend_run_dir, port).write_text("stale-from-a-prior-boot")

    with pytest.raises(BackendReadyTimeout) as exc:
        wait_until_ready(s, timeout=0.6, poll_interval=0.05)
    assert "secret_present=True" in str(exc.value)
    assert "port_open=False" in str(exc.value)


def test_wait_until_ready_times_out_when_port_open_but_no_secret(tmp_path):
    # A live port with no secret is NOT ready either.
    port = free_port()
    s = make_settings(tmp_path, port)
    script = write_fake_backend(tmp_path)
    # Fake binds the port but never writes a secret (no run-dir given).
    pg = start_backend(s, argv=fake_argv(script, port=port, ttl=30))
    try:
        with pytest.raises(BackendReadyTimeout) as exc:
            wait_until_ready(s, timeout=1.0, process=pg, poll_interval=0.05)
        assert "port_open=True" in str(exc.value)
        assert "secret_present=False" in str(exc.value)
    finally:
        pg.terminate(2.0)


def test_wait_until_ready_reports_backend_exit_without_waiting_out_timeout(tmp_path):
    port = free_port()
    s = make_settings(tmp_path, port)
    script = write_fake_backend(tmp_path)
    # ttl=0.2 so the fake exits almost immediately, never opening long.
    pg = start_backend(s, argv=fake_argv(script, ttl=0.2))
    t0 = time.monotonic()
    with pytest.raises(BackendExited):
        wait_until_ready(s, timeout=30.0, process=pg, poll_interval=0.05)
    assert time.monotonic() - t0 < 5.0, "should fail fast on exit, not wait 30s"


def test_start_backend_creates_the_run_directory(tmp_path):
    port = free_port()
    s = make_settings(tmp_path, port)
    assert not s.backend_run_dir.exists()
    script = write_fake_backend(tmp_path)
    pg = start_backend(s, argv=fake_argv(script, ttl=0.5))
    try:
        assert s.backend_run_dir.exists()
    finally:
        pg.terminate(2.0)
