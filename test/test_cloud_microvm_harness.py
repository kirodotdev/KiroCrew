"""The harness's own side effects: what it leaves on disk, and how it takes ports.

These are not tests of the lane. They are tests of the thing that tests the lane,
and they exist because both defects they pin are invisible from a green run: a
harness that leaks its scratch tree only leaks it when it FAILS, and a harness
that reserves a port before binding it only collides when something else on the
machine is quick.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

#: The harness package, loaded under a name of our own.
#:
#: Not ``import test.microvm_harness``. The repo's ``test/`` directory is not a
#: package and the standard library HAS a package called ``test``, so whichever
#: of the two is registered first wins for the rest of the worker -- which makes
#: a plain absolute import here depend on what else the shard imported before
#: this module, and it failed in CI for exactly that reason. Loading the package
#: under a name nothing else uses removes the question, and the harness's own
#: modules import each other relatively so they do not care what it is called.
_HARNESS = "kirocrew_microvm_harness"
_HARNESS_DIR = Path(__file__).resolve().parent / "microvm_harness"


def _load_harness() -> None:
    if _HARNESS in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        _HARNESS,
        _HARNESS_DIR / "__init__.py",
        submodule_search_locations=[str(_HARNESS_DIR)],
    )
    assert spec and spec.loader, f"the harness package is not at {_HARNESS_DIR}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_HARNESS] = module
    spec.loader.exec_module(module)


_load_harness()
e2e = importlib.import_module(f"{_HARNESS}.e2e")
fake = importlib.import_module(f"{_HARNESS}.fake_microvm_endpoint")

FakeControlPlane = fake.FakeControlPlane
FakeMicroVmEndpoint = fake.FakeMicroVmEndpoint
Result = e2e.Result
free_port = fake.free_port
scratch_dir = e2e.scratch_dir


class TestScratchDirectory:
    def test_it_is_removed_when_the_cycle_succeeds(self):
        result = Result()
        with scratch_dir(result, keep=False) as work:
            (work / "credentials").write_text("secret\n")
            held = work
        assert not held.exists()

    def test_it_is_removed_when_the_cycle_RAISES(self):
        """The case a cleanup placed after the run misses. The scratch tree holds
        the harness's credential and config files, so a step that raises must not
        leave them on disk while the run reports failure and says nothing about
        them."""
        result = Result()
        held: list[Path] = []
        with pytest.raises(RuntimeError, match="scripted"):
            with scratch_dir(result, keep=False) as work:
                (work / "credentials").write_text("secret\n")
                held.append(work)
                raise RuntimeError("scripted failure mid-cycle")
        assert held and not held[0].exists()

    def test_keep_preserves_it_and_records_where(self):
        result = Result()
        with scratch_dir(result, keep=True) as work:
            held = work
        assert held.exists()
        assert result.facts["work_dir"] == str(held)
        import shutil

        shutil.rmtree(held, ignore_errors=True)

    def test_keep_preserves_it_even_when_the_cycle_raises(self):
        """A failed run is exactly when someone passes --keep, so the directory
        has to survive the failure that made them ask for it."""
        result = Result()
        held: list[Path] = []
        with pytest.raises(RuntimeError):
            with scratch_dir(result, keep=True) as work:
                held.append(work)
                raise RuntimeError("scripted failure mid-cycle")
        assert held[0].exists()
        assert result.facts["work_dir"] == str(held[0])
        import shutil

        shutil.rmtree(held[0], ignore_errors=True)


class TestPortsAreNotReservedThenHandedOver:
    """A port bound, released, and handed to a second binder is a race.

    The window between the release and the second bind is one another listener on
    the machine can take, and the result is an address-in-use at startup that says
    nothing about the test. Each site either binds once or retries.
    """

    def test_the_fake_endpoint_binds_port_zero_and_reads_it_back(self):
        with FakeMicroVmEndpoint(engine=_NoopEngine()) as endpoint:
            assert endpoint.port > 0
            # The listening socket IS the one that chose the port, so the two
            # cannot disagree.
            assert endpoint.port == endpoint._server.server_address[1]
            assert endpoint.endpoint_url.endswith(str(endpoint.port))

    def test_two_endpoints_do_not_collide(self):
        with FakeMicroVmEndpoint(engine=_NoopEngine()) as first:
            with FakeMicroVmEndpoint(engine=_NoopEngine()) as second:
                assert first.port != second.port

    def test_the_fake_leaves_the_guests_port_to_docker(self):
        """Zero, not a reserved port: the engine reads the answer back from the
        started container, which is one atomic allocation."""
        plane = FakeControlPlane(engine=_NoopEngine())
        vm = plane.run({"runHookPayload": "", "maximumDurationInSeconds": 900})
        assert vm.host_port == 0

    def test_free_port_still_answers_a_port_nothing_holds(self):
        """Kept because the fake endpoint's own bind-zero path reads it back, and a
        helper nothing exercises is a helper that rots."""
        assert free_port() > 0


class _NoopEngine:
    """Enough of ``LocalLaunchEngine`` for a test that must not touch docker."""

    def launch(self, tag, *, host_port=0, generation=1):
        raise AssertionError("this test must not start a container")

    def reap(self):
        return []
