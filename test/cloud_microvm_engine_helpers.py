"""The launch spec, scripted launcher, secret stub and engine fixture the engine tests share.

``SPEC`` is a ``MicroVmLaunchSpec`` pinned to a prebuilt image. ``FakeLauncher`` is a
scripted ``VmLauncher`` that fails at the step a test names, answers a reconcile with a
scripted VM or ``None``, and records the order of its calls, the profile and region of its
activation and teardown calls, and the payloads and client tokens it is sent.
``_no_real_secret_writes`` is an autouse fixture that stubs the secret put and delete so
no test reaches Secrets Manager. The ``engine`` fixture builds a ``MicroVmLaunchEngine``
over ``SPEC``, a temporary store and a ``FakeLauncher``, with the activation delete and
the node deregistration stubbed to record themselves in the launcher's calls, and returns
all three. ``test_cloud_microvm_engine.py`` imports all four,
``test_cloud_microvm_engine_lifecycle.py`` all but ``FakeLauncher``, and
``test_cloud_microvm_engine_image.py`` ``SPEC`` and the secret stub.
"""

from __future__ import annotations

import pytest

from kiro_crew.cloud.microvm import api
from kiro_crew.cloud.microvm.engine import (
    Activation,
    MicroVmLaunchEngine,
    MicroVmLaunchSpec,
    VmLauncher,
)
from kiro_crew.cloud.microvm.record import CrewStore

SPEC = MicroVmLaunchSpec(
    image_identifier="arn:aws:lambda:us-east-1:123456789012:microvm-image/kirocrew",
    image_version="7",
    kms_key_id="arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555",
    identity_secret_ref="kirocrew/identity/demo-crew",
    wall_seconds=900,
)


class FakeLauncher(VmLauncher):
    """A scripted platform that records the ORDER it was asked to do things."""

    #: What the idempotency replay answers with. A VM by default, because that is
    #: the case the lane has to survive: the service accepted the create and this
    #: process never saw the id. ``None`` is the other real outcome -- the call
    #: never reached the service -- and a test sets it to ask for that one.
    RECONCILED = api.MicroVm(
        microvm_id="mvm-recovered",
        state="PENDING",
        endpoint="https://mvm-recovered.example/",
    )

    def __init__(self, *, fail_at: str = ""):
        self.calls: list[str] = []
        self.fail_at = fail_at
        self.payloads: list[str] = []
        self.client_tokens: list[str] = []
        self.reconcile_answer: object = self.RECONCILED
        #: ``(call, profile, region)`` per call, so a test can assert WHERE a call
        #: went and not only that it happened. A teardown sent to the wrong
        #: region fails quietly and still marks the row terminated.
        self.coords: list[tuple[str, str, str]] = []

    def _maybe_fail(self, step: str) -> None:
        if self.fail_at == step:
            raise RuntimeError(f"scripted failure at {step}")

    def create_activation(self, *, tag, profile, region):
        self.calls.append("create_activation")
        self.coords.append(("create_activation", profile, region))
        self._maybe_fail("create_activation")
        return Activation(activation_id="act-1", activation_code="code-1")

    def run(self, *, payload, profile, region, client_token):
        self.calls.append("run")
        self.payloads.append(payload)
        self.client_tokens.append(client_token)
        self._maybe_fail("run")
        return api.MicroVm(
            microvm_id="mvm-1",
            state="PENDING",
            endpoint="https://mvm-1.example/",
            started_at="2026-10-06T21:00:00Z",
        )

    def reconcile(self, *, payload, profile, region, client_token):
        """Overridden rather than inherited, so the test can see it happen.

        The base class implements it as a replay of :meth:`run`, which here would
        hit ``fail_at`` again and report nothing about the account. Scripted
        instead: the token's VM, or ``None`` for a call the service never got.
        """
        self.calls.append("reconcile")
        self.client_tokens.append(client_token)
        self._maybe_fail("reconcile")
        return self.reconcile_answer

    def wait_online(self, *, activation_id, profile, region):
        self.calls.append("wait_online")
        self._maybe_fail("wait_online")
        return "mi-0123456789abcdef0"

    def terminate(self, microvm_id, *, profile, region):
        self.calls.append("terminate")
        self.coords.append(("terminate", profile, region))

    def wait_terminated(self, microvm_id, *, profile, region):
        self.calls.append("wait_terminated")
        self.coords.append(("wait_terminated", profile, region))


@pytest.fixture(autouse=True)
def _no_real_secret_writes(_floor_monkeypatch):
    """The launch mints the crew's control secret, which is a real AWS write.

    Stubbed for every test in this file: what these tests are about is the ORDER
    and the records, and a test that reached Secrets Manager would be a test that
    needs an account.
    """
    from kiro_crew.cloud.microvm import engine as engine_mod

    _floor_monkeypatch.setattr(
        engine_mod.api, "put_secret", lambda name, value, **kw: f"arn:fake:{name}"
    )
    _floor_monkeypatch.setattr(engine_mod.api, "delete_secret", lambda name, **kw: None)


@pytest.fixture(name="engine")
def engine_fixture(tmp_path, monkeypatch):
    store = CrewStore(tmp_path / "crews.json")
    launcher = FakeLauncher()
    monkeypatch.setattr(
        "kiro_crew.cloud.microvm.engine._delete_activation",
        lambda activation_id, *, profile, region: launcher.calls.append("delete_activation"),
    )
    # Both AWS mutations a teardown makes are stubbed here, so no test that uses
    # this fixture can reach a real ``aws ssm`` call on a machine with a profile.
    monkeypatch.setattr(
        "kiro_crew.cloud.microvm.engine._deregister_node",
        lambda mi_id, *, profile, region: launcher.calls.append("deregister_node"),
    )
    built = MicroVmLaunchEngine(spec=SPEC, store=store, launcher=launcher)
    return built, store, launcher
