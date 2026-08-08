"""Unit tests for the durable cloud launch-job model (cloud/launch_job.py).

No AWS: the launch engine is a configurable fake, so these exercise the state
machine, disk durability, and the device-code / cancel / failure paths.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat
from kiro_crew.cloud import launch_job as lj


class FakeHandle:
    def __init__(self, *, already=False, url="", code="", ports=None, signed=True, on_wait=None):
        self.already_logged_in = already
        self.url = url
        self.code = code
        self.ports = ports or []
        self._signed = signed
        self._on_wait = on_wait
        self.closed = False

    def wait(self, cancel: threading.Event) -> bool:
        if self._on_wait is not None:
            self._on_wait(cancel)
        return self._signed

    def close(self) -> None:
        self.closed = True


class FakeEngine:
    """Records calls; each step is individually configurable to raise/return."""

    def __init__(self, *, handle=None, preflight_exc=None, provision_exc=None, register_exc=None,
                 teardown_exc=None, teardown_confirms=True):
        self.handle = handle or FakeHandle(already=True)
        self.preflight_exc = preflight_exc
        self.provision_exc = provision_exc
        self.register_exc = register_exc
        self.teardown_exc = teardown_exc
        self.teardown_confirms = teardown_confirms
        self.calls: list = []

    def teardown(self, *, tag, profile, region):
        self.calls.append(("teardown", tag))
        if self.teardown_exc:
            raise self.teardown_exc
        return self.teardown_confirms

    def preflight(self, profile, region):
        self.calls.append(("preflight", profile, region))
        if self.preflight_exc:
            raise self.preflight_exc

    def provision(self, *, tag, size_key, profile, region):
        self.calls.append(("provision", tag, size_key))
        if self.provision_exc:
            raise self.provision_exc
        return "i-0abc123456789def0"

    def begin_signin(self, *, instance_id, profile, region):
        self.calls.append(("begin_signin", instance_id))
        return self.handle

    def register(self, *, instance_id, tag, profile, region):
        self.calls.append(("register", instance_id, tag))
        if self.register_exc:
            raise self.register_exc


def _store(tmp_path):
    return lj.LaunchJobStore(root=tmp_path / "launch-jobs")


class TestStoreDurability:
    def test_create_persists_and_fresh_store_sees_it(self, tmp_path):
        s1 = _store(tmp_path)
        job = s1.create(profile="dev", region="us-east-1", size_key="balanced")
        assert job.status == lj.PENDING
        # A brand-new store instance reading the same root sees it (survives restart).
        s2 = lj.LaunchJobStore(root=s1.root)
        loaded = s2.get(job.id)
        assert loaded is not None
        assert loaded.size_key == "balanced"
        assert [st.key for st in loaded.steps] == [
            lj.STEP_PREFLIGHT,
            lj.STEP_PROVISION,
            lj.STEP_SIGNIN,
            lj.STEP_CONNECT,
        ]

    def test_create_rejects_unknown_size(self, tmp_path):
        with pytest.raises(KeyError):
            _store(tmp_path).create(profile="dev", region="us-east-1", size_key="nope")

    def test_get_retries_a_transient_read_error(self, tmp_path, monkeypatch):
        # A concurrent save() replaces the file via os.replace; on Windows a read
        # colliding with the in-progress replace raises a sharing violation (OSError).
        # get() must retry so list() never silently drops an in-flight job. (POSIX
        # replace is atomic and reads never error, so this path is Windows-only.)
        import pathlib

        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        real_read = pathlib.Path.read_text
        calls = {"n": 0}

        def flaky(self, *a, **k):
            if str(self).endswith(f"{job.id}.json") and calls["n"] < 2:
                calls["n"] += 1
                raise OSError("simulated Windows sharing violation")
            return real_read(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, "read_text", flaky)
        got = s.get(job.id)
        assert got is not None and got.id == job.id
        assert calls["n"] == 2  # it retried past the two transient failures

    def test_round_trip_preserves_signin_prompt(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        job.signin = lj.SigninPrompt(url="https://x/verify", code="BQTZ-XKFD", ports=[54123])
        job.status = lj.AWAITING_SIGNIN
        s.save(job)
        back = lj.LaunchJobStore(root=s.root).get(job.id)
        assert back.status == lj.AWAITING_SIGNIN
        assert back.signin.code == "BQTZ-XKFD"
        assert back.signin.ports == [54123]

    def test_list_and_delete(self, tmp_path):
        s = _store(tmp_path)
        a = s.create(profile="dev", region="us-east-1", size_key="light")
        s.create(profile="dev", region="us-east-1", size_key="power")
        assert len(s.list()) == 2
        assert s.delete(a.id) is True
        assert s.get(a.id) is None
        assert len(s.list()) == 1

    def test_bad_job_id_cannot_escape_store(self, tmp_path):
        s = _store(tmp_path)
        assert s.get("../etc/passwd") is None
        assert s.delete("../../x") is False
        # A charset-valid but over-long id must be rejected BEFORE any filesystem
        # access — otherwise Path.exists() raises ENAMETOOLONG (HTTP 500) instead of a
        # clean not-found. get() maps the ValueError to None; _path raises it.
        huge = "a" * 300
        assert s.get(huge) is None
        with pytest.raises(ValueError):
            s._path(huge)


class TestRunLaunch:
    def test_happy_path_already_signed_in(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        eng = FakeEngine(handle=FakeHandle(already=True))
        out = lj.run_launch(job, s, eng)
        assert out.status == lj.DONE
        assert out.instance_id == "i-0abc123456789def0"
        assert out.tag.startswith("kc-")
        assert all(st.state == lj.STEP_DONE for st in out.steps)
        assert out.signin_detected is True
        assert [c[0] for c in eng.calls] == ["preflight", "provision", "begin_signin", "register"]
        # Persisted terminal state is visible to a fresh reader.
        assert lj.LaunchJobStore(root=s.root).get(job.id).status == lj.DONE

    def test_device_code_awaiting_then_signed(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        seen = {}

        def on_wait(_cancel):
            # While the human is approving, the on-disk job must be AWAITING_SIGNIN
            # with the code visible — that is what the UI renders across a reload.
            mid = lj.LaunchJobStore(root=s.root).get(job.id)
            seen["status"] = mid.status
            seen["code"] = mid.signin.code if mid.signin else None

        eng = FakeEngine(
            handle=FakeHandle(url="https://x/verify", code="BQTZ-XKFD", ports=[54123], signed=True,
                              on_wait=on_wait)
        )
        out = lj.run_launch(job, s, eng)
        assert seen["status"] == lj.AWAITING_SIGNIN
        assert seen["code"] == "BQTZ-XKFD"
        assert out.status == lj.DONE
        assert out.signin_detected is True
        assert out.signin is None  # cleared after sign-in resolves
        assert out.step(lj.STEP_SIGNIN).state == lj.STEP_DONE
        assert eng.handle.closed is True

    def test_device_code_not_detected_still_registers(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        eng = FakeEngine(handle=FakeHandle(url="https://x/verify", code="AAAA", signed=False))
        out = lj.run_launch(job, s, eng)
        # Not signed in is not fatal — the box still registers so the user can
        # finish sign-in from the dashboard.
        assert out.status == lj.DONE
        assert out.signin_detected is False
        assert out.step(lj.STEP_SIGNIN).state == lj.STEP_SKIPPED
        assert out.step(lj.STEP_CONNECT).state == lj.STEP_DONE
        assert ("register", "i-0abc123456789def0", out.tag) in eng.calls

    def test_provision_failure_marks_failed(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        eng = FakeEngine(provision_exc=RuntimeError("AccessDenied: ec2:RunInstances"))
        out = lj.run_launch(job, s, eng)
        assert out.status == lj.FAILED
        assert "AccessDenied" in out.error
        assert out.step(lj.STEP_PREFLIGHT).state == lj.STEP_DONE
        assert out.step(lj.STEP_PROVISION).state == lj.STEP_FAILED
        assert out.step(lj.STEP_SIGNIN).state == lj.STEP_PENDING
        assert "begin_signin" not in [c[0] for c in eng.calls]
        # A provision failure can leave a running, billing stack — it must be rolled
        # back (best-effort) so a transient post-deploy error can't orphan it.
        assert ("teardown", out.tag) in eng.calls
        assert "was removed" in out.error

    def test_a_failure_after_provisioning_does_not_tear_down_the_crew(self, tmp_path):
        # Once provisioning succeeded the crew exists; a later-step failure (register)
        # must NOT delete it — register even names it so the user can recover it. Only
        # a STEP_PROVISION failure rolls back.
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        eng = FakeEngine(
            handle=FakeHandle(already=True),
            register_exc=RuntimeError("could not add to your crews (billing)"),
        )
        out = lj.run_launch(job, s, eng)
        assert out.status == lj.FAILED
        assert out.step(lj.STEP_PROVISION).state == lj.STEP_DONE
        assert not any(c[0] == "teardown" for c in eng.calls)

    def test_cancel_before_provision(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        cancel = threading.Event()
        cancel.set()  # cancelled before it even starts
        out = lj.run_launch(job, s, FakeEngine(), cancel=cancel)
        assert out.status == lj.CANCELLED
        assert out.instance_id == ""

    def test_cancel_during_signin_wait(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")

        def cancel_mid(cancel: threading.Event):
            cancel.set()  # the user cancels while we wait for the code approval

        eng = FakeEngine(
            handle=FakeHandle(url="https://x/verify", code="AAAA", signed=False, on_wait=cancel_mid)
        )
        out = lj.run_launch(job, s, eng, cancel=threading.Event())
        assert out.status == lj.CANCELLED
        assert out.signin is None
        # register must NOT have run after a cancel.
        assert "register" not in [c[0] for c in eng.calls]

    def test_run_launch_on_terminal_job_is_noop(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        job.status = lj.DONE
        eng = FakeEngine()
        out = lj.run_launch(job, s, eng)
        assert out.status == lj.DONE
        assert eng.calls == []


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="POSIX mode bits are not enforced on Windows (a file reads 0o666, a dir 0o777), "
    "and the cloud routes are POSIX-only anyway — handlers_cloud._guard rejects win32.",
)
class TestFilePermissions:
    def test_a_parked_job_is_not_readable_by_other_local_users(self, tmp_path):
        """A job parked at the sign-in step holds the device code until the human
        approves it. Under umask 022 the file would be 0644 and any other local
        account could read the code and redeem the login."""
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        job.signin = lj.SigninPrompt(url="https://device.sso/x", code="WXYZ-1234", ports=[])
        s.save(job)

        mode = (s.root / f"{job.id}.json").stat().st_mode & 0o777
        assert mode == 0o600, f"job file is {oct(mode)}, expected 0o600"
        assert s.root.stat().st_mode & 0o777 == 0o700

    def test_a_pre_existing_wide_open_store_dir_is_tightened(self, tmp_path):
        s = _store(tmp_path)
        s.root.mkdir(parents=True)
        s.root.chmod(0o755)  # what an earlier build would have left
        s.save(s.create(profile="dev", region="us-east-1", size_key="balanced"))
        assert s.root.stat().st_mode & 0o777 == 0o700

    def test_default_store_root_is_on_the_sensitive_path_floor(self):
        """A job awaiting sign-in persists the device-login URL + code — a credential.
        The default store must live under the ``run/`` tree, which is on the shared
        sensitive-path floor, so agent file tools cannot read (and exfiltrate) it.
        (Same-UID file perms alone don't help: agent tools run as the same user.)"""
        from kiro_crew import security

        store = lj.LaunchJobStore()  # default root; constructing it touches no disk
        assert store.root.parent.name == "run"
        assert store.root.name == "cloud-launch-jobs"
        assert security.is_sensitive_path(str(store.root)) is True
        assert security.is_sensitive_path(str(store.root / "deadbeef1234.json")) is True


class TestRealSigninHandleFailures:
    """A sign-in that cannot be confirmed must not fail the job: run_launch would
    return before STEP_CONNECT, leaving a provisioned, billing instance that was
    never registered and so never appears in the crew list."""

    def _handle(self, monkeypatch, resume_exc=None):
        from kiro_crew.cloud import launch_engine as le

        monkeypatch.setattr(
            le.login, "start_device_login",
            lambda *a, **k: SimpleNamespace(
                already_logged_in=False, url="u", code="c", ports=[], close=lambda: None
            ),
        )

        def _resume(*a, **k):
            if resume_exc:
                raise resume_exc

        monkeypatch.setattr(le.login, "resume_login_daemon", _resume)
        monkeypatch.setattr(le.login, "wait_until_logged_in", lambda *a, **k: False)
        return le._RealSigninHandle("i-0abc", "", "us-east-1")

    def test_an_aws_failure_resuming_login_is_unconfirmed_not_fatal(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        h = self._handle(monkeypatch, resume_exc=AWSError("ssm hiccup"))
        assert h.wait(threading.Event()) is False

    def test_a_non_aws_failure_is_also_survivable(self, monkeypatch):
        # These helpers shell out, so an exec/sandbox failure arrives as an
        # unrelated exception type — it must not strand the crew either.
        h = self._handle(monkeypatch, resume_exc=RuntimeError("no sandbox backend"))
        assert h.wait(threading.Event()) is False

    def test_a_failure_starting_device_login_does_not_strand_the_crew(self, monkeypatch):
        # start_device_login shells out to SSM. If it raises in the constructor, the
        # job used to fail BEFORE register() — stranding a provisioned, billing
        # instance outside the crew list. The handle must instead come back empty and
        # unconfirmed so the launch still registers the crew.
        from kiro_crew.cloud import launch_engine as le

        def _boom(*a, **k):
            raise RuntimeError("transient ssm send-command failure")

        monkeypatch.setattr(le.login, "start_device_login", _boom)
        h = le._RealSigninHandle("i-0abc", "", "us-east-1")  # must NOT raise
        assert h.already_logged_in is False
        assert h.url == ""
        assert h.code == ""
        assert h.ports == []
        h.close()  # tolerates the absent prompt


class TestRealEngineGatewayPort:
    """The tunnel forces local_port == remote_port and hard-fails when that port is
    busy, so registering every crew on the default 5476 produced a crew that could
    never be connected — the operator's own gateway usually owns 5476."""

    def _engine(self, monkeypatch, used_ports):
        from kiro_crew.cloud import launch_engine as le

        seen = {}
        monkeypatch.setattr(le.ec2, "deploy", lambda **kw: (
            seen.update(kw) or SimpleNamespace(instance_id="i-0abc")))
        monkeypatch.setattr(le.sizes, "get_tier", lambda k: SimpleNamespace(key=k))
        monkeypatch.setattr(le.source_mod, "find_repo_root", lambda: None)
        monkeypatch.setattr(
            le.connect_mod, "register_instance",
            lambda iid, **kw: seen.update({"reg": kw}) or "inst-1",
        )
        # Deterministic allocation: the first free port at/after the base, minus
        # whatever the registry already hands out.
        monkeypatch.setattr(
            le, "PortAllocator",
            lambda *a, **k: SimpleNamespace(
                allocate=lambda exclude=None: next(
                    p for p in range(5600, 5700) if p not in set(exclude or ())
                )
            ),
        )
        return le, seen

    def test_the_same_allocated_port_reaches_the_stack_and_the_registry(self, monkeypatch):
        le, seen = self._engine(monkeypatch, set())
        eng = le.RealLaunchEngine()

        eng.provision(tag="kc-1", size_key="balanced", profile="", region="us-east-1")
        eng.register(instance_id="i-0abc", tag="kc-1", profile="", region="us-east-1")

        # One port, both ends — a mismatch would forward the tunnel at nothing.
        assert seen["dashboard_port"] == 5600
        assert seen["reg"]["remote_port"] == 5600

    def test_it_skips_ports_the_registry_already_uses(self, monkeypatch):
        le, seen = self._engine(monkeypatch, set())

        class _Reg:
            def list(self):
                return [SimpleNamespace(remote_port=5600, local_port=5601)]

        # The import is module-scope now, so the name must be patched WHERE IT IS
        # LOOKED UP — patching sys.modules would be a no-op.
        monkeypatch.setattr(le, "InstancesRegistry", _Reg)
        le.RealLaunchEngine().provision(
            tag="kc-2", size_key="balanced", profile="", region="us-east-1"
        )

        assert seen["dashboard_port"] == 5602

    def test_a_registry_read_failure_does_not_block_the_launch(self, monkeypatch):
        le, seen = self._engine(monkeypatch, set())

        class _Boom:
            def __init__(self):
                raise RuntimeError("registry unreadable")

        monkeypatch.setattr(le, "InstancesRegistry", _Boom)
        le.RealLaunchEngine().provision(
            tag="kc-3", size_key="balanced", profile="", region="us-east-1"
        )

        assert seen["dashboard_port"] == 5600


class TestRealEngineProvisionSourceMode:
    """One-click setup must work from a wheel/app install. `source.repo_root()` fails
    closed there by design (it must never tar up site-packages), so leaving
    ship_source at its default made provisioning raise for every user who did not
    install from git."""

    def _deploy_spy(self, monkeypatch):
        from kiro_crew.cloud import launch_engine as le

        seen = {}

        def _fake_deploy(**kw):
            seen.update(kw)
            return SimpleNamespace(instance_id="i-0abc")

        monkeypatch.setattr(le.ec2, "deploy", _fake_deploy)
        monkeypatch.setattr(le.sizes, "get_tier", lambda k: SimpleNamespace(key=k))
        return le, seen

    def test_without_a_checkout_it_clones_instead_of_failing(self, monkeypatch):
        le, seen = self._deploy_spy(monkeypatch)
        monkeypatch.setattr(le.source_mod, "find_repo_root", lambda: None)

        got = le.RealLaunchEngine().provision(
            tag="kc-1", size_key="balanced", profile="", region="us-east-1"
        )

        assert got == "i-0abc"
        assert seen["ship_source"] is False

    def test_with_a_checkout_it_still_ships_local_source(self, monkeypatch, tmp_path):
        le, seen = self._deploy_spy(monkeypatch)
        monkeypatch.setattr(le.source_mod, "find_repo_root", lambda: tmp_path)

        le.RealLaunchEngine().provision(
            tag="kc-1", size_key="balanced", profile="", region="us-east-1"
        )

        assert seen["ship_source"] is True


class TestRealEngineRegistration:
    """`connect.register_instance` is best-effort by contract — it returns None on a
    registry failure instead of raising. The engine must not treat that as success."""

    def test_a_registry_failure_is_not_reported_as_a_finished_launch(self, monkeypatch):
        from kiro_crew.cloud import launch_engine as le

        monkeypatch.setattr(le.connect_mod, "register_instance", lambda *a, **k: None)

        with pytest.raises(RuntimeError) as err:
            le.RealLaunchEngine().register(
                instance_id="i-0abc", tag="kc-3f9a", profile="", region="us-east-1"
            )

        # The instance exists and is billing, so the message must name it.
        assert "i-0abc" in str(err.value)
        assert "kc-3f9a" in str(err.value)
        assert "billing" in str(err.value)

    def test_a_successful_registration_returns_quietly(self, monkeypatch):
        from kiro_crew.cloud import launch_engine as le

        monkeypatch.setattr(le.connect_mod, "register_instance", lambda *a, **k: "inst-7")

        le.RealLaunchEngine().register(
            instance_id="i-0abc", tag="kc-3f9a", profile="", region="us-east-1"
        )


class TestCancelRollsBackTheStack:
    """Cancellation is only observed between steps and the crew is registered last, so
    a cancel after provisioning must delete the stack — otherwise a billing instance
    survives that the dashboard never lists."""

    def _cancel_at_signin(self, tmp_path, **engine_kw):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        cancel = threading.Event()

        def _on_wait(ev):
            cancel.set()  # the human gives up while the device code is pending

        eng = FakeEngine(handle=FakeHandle(url="u", code="c", on_wait=_on_wait), **engine_kw)
        out = lj.run_launch(job, s, eng, cancel=cancel)
        return out, eng

    def test_a_cancel_after_provisioning_deletes_the_stack(self, tmp_path):
        out, eng = self._cancel_at_signin(tmp_path)

        assert out.status == lj.CANCELLED
        assert ("teardown", out.tag) in eng.calls
        assert out.step(lj.STEP_PROVISION).detail == f"Removed {out.tag} after cancellation."

    def test_an_unconfirmed_delete_is_not_reported_as_removed(self, tmp_path):
        """`destroy(wait=False)` only means the request was accepted. A stack that
        later reaches DELETE_FAILED must not leave the job claiming "Removed" — the
        user would believe billing stopped while the instance is still up."""
        out, _ = self._cancel_at_signin(tmp_path, teardown_confirms=False)

        assert out.status == lj.CANCELLED
        detail = out.step(lj.STEP_PROVISION).detail or ""
        assert "Removed" not in detail
        assert out.tag in (out.error or "")
        assert "did NOT confirm" in (out.error or "")
        assert "billing" in (out.error or "")

    def test_a_failed_rollback_names_the_stack_so_the_user_can_remove_it(self, tmp_path):
        out, _ = self._cancel_at_signin(tmp_path, teardown_exc=RuntimeError("access denied"))

        assert out.status == lj.CANCELLED
        assert out.tag in (out.error or ""), "the user must be told what to delete"
        assert "billing" in (out.error or "")

    def test_a_cancel_before_provisioning_deletes_nothing(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        cancel = threading.Event()
        cancel.set()  # cancelled before the first step runs

        eng = FakeEngine()
        out = lj.run_launch(job, s, eng, cancel=cancel)

        assert out.status == lj.CANCELLED
        assert not any(c[0] == "teardown" for c in eng.calls)
        assert not any(c[0] == "provision" for c in eng.calls)


class TestSigninPromptRetention:
    def test_an_unsigned_wait_keeps_the_code_the_message_tells_you_to_use(self, tmp_path):
        """When the wait runs out the step says "finish it from the dashboard" — which
        is only possible if the dashboard still has the URL and code. Clearing the
        prompt here is what made that instruction a dead end."""
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")

        class UnsignedEngine(FakeEngine):
            def begin_signin(self, *, instance_id, profile, region):
                return FakeHandle(url="https://device.sso/verify", code="WXYZ-1234", signed=False)

        out = lj.run_launch(job, s, UnsignedEngine())

        assert out.signin_detected is False
        assert out.signin is not None, "the device code must survive an unsigned wait"
        assert out.signin.code == "WXYZ-1234"
        assert out.step(lj.STEP_SIGNIN).state == lj.STEP_SKIPPED


class TestOrphanReaping:
    def test_a_freshly_created_job_survives_a_concurrent_reap(self, tmp_path):
        """The reap runs off the event loop, so it can list the job dir between
        create() and the worker's adopt(). If it terminalized the new job, the
        "already running" guard would stop seeing it and the user's retry would
        provision — and bill for — a second stack."""
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")

        reaped = s.reap_orphans()  # as if a concurrent request triggered it now

        assert job.id not in reaped
        assert s.get(job.id).status != lj.FAILED

    """A launch runs on a daemon thread, so a restart kills the worker while the
    file still says ``running``. A fresh store must not leave that job pending
    forever — the UI would poll a card that can never advance."""

    def test_a_job_left_running_by_a_dead_gateway_is_terminalized(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        job.status = lj.RUNNING
        job.step(lj.STEP_PROVISION).state = lj.STEP_ACTIVE
        s.save(job)

        # A NEW store models the next gateway process: it owns nothing.
        reaped = _store(tmp_path).reap_orphans()

        assert reaped == [job.id]
        after = _store(tmp_path).get(job.id)
        assert after is not None
        assert after.status == lj.FAILED
        assert after.terminal
        assert "restarted" in (after.error or "")
        # the step that was mid-flight must not still read as active
        assert after.step(lj.STEP_PROVISION).state == lj.STEP_FAILED

    def test_a_job_this_process_owns_is_left_alone(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        job.status = lj.RUNNING
        s.save(job)
        s.adopt(job.id)  # a worker here is driving it

        assert s.reap_orphans() == []
        still = s.get(job.id)
        assert still is not None and still.status == lj.RUNNING

    def test_terminal_jobs_are_untouched(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        job.status = lj.DONE
        s.save(job)
        assert _store(tmp_path).reap_orphans() == []
        after = _store(tmp_path).get(job.id)
        assert after is not None and after.status == lj.DONE
