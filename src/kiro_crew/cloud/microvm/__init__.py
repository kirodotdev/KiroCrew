"""The Lambda MicroVM remote-crew lane.

A crew on this lane runs inside a Firecracker MicroVM that the platform
terminates at a non-adjustable maximum lifetime. That clock is the shape of the
lane: the compute goes away whether or not anyone is finished with it, so this
lane launches a crew, reaches it over SSM, and serves its turns while it lives.
The crew's home lives on the VM's own disk and goes away with it.

Keeping that home -- archiving it, restoring it on reopen, and suspending an idle
crew instead of paying for it -- is a separate concern and a separate change. It
is not here.

The parts, and which question each answers:

``states``
    Where a crew can be, and the only function that moves it.
``record``
    What the control plane remembers about one crew, and where that is stored.
``api``
    The ``lambda-microvms`` calls, through the one ``aws`` CLI chokepoint.
``image``
    A crew's image is BUILT from an AWS-managed base plus that crew's own signed
    bundle, and cached by a digest of those two inputs. Identity is the inputs,
    because a name that merely labels a build lets a cache serve the wrong content.
``recipe``
    What goes IN that build: the Fargate lane's own ``Dockerfile.crew`` plus the
    crew bundle, zipped. A join rather than a second path -- the Dockerfile, the
    required layout and the bundle digest are all read from the lane that already
    answered them.
``payload``
    What the platform hands the guest at boot.
``engine``
    The five-method ``LaunchEngine``.

The docker-backed stand-in that makes all of the above testable with no AWS account
is NOT here: it drives ``docker`` with a caller-supplied argv, which does not belong in
the shipped package, so it lives beside the tests that use it in
``test/microvm_harness/local_engine.py``.

Fargate is untouched by any of it. Every lane-neutral piece -- the launch step
machine, the progress UI, the instances registry, the SSM port-forward -- is
reused in place rather than moved, so this lane's review is about this lane.
"""

from __future__ import annotations

from kiro_crew.cloud.microvm.engine import (
    MICROVM_PROVISIONER_ID,
    MicroVmLaunchEngine,
    MicroVmLaunchSpec,
)
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

__all__ = [
    "MICROVM_PROVISIONER_ID",
    "CrewRecord",
    "CrewStore",
    "MicroVmLaunchEngine",
    "MicroVmLaunchSpec",
]
