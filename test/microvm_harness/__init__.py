"""The local harness for the MicroVM lane. No AWS account, no credential.

The lane has two halves that fail in different ways, so the harness has two
layers and each one is the cheapest thing that can exercise its half honestly:

**Layer 1 -- the guest: docker running the published crew image.**
:mod:`test.microvm_harness.local_engine`. ``docker run`` / ``rm -f`` against the
same image the real lane runs, so the crew's own turn and the state it writes to
its home are real rather than simulated.

**Layer 2 -- the control plane: a loopback fake.**
:mod:`test.microvm_harness.fake_microvm_endpoint`. moto has no MicroVM backend, so
this is the only layer that has to be written -- and the shapes it serves are
pinned against botocore's installed model by
:mod:`test.test_cloud_microvm_contract`, not invented here.

**What the harness cannot prove, and must never be read as proving.** The ceiling
is in ``R2``'s own words and it is not a formality: no real provisioning latency
or capacity pressure, no real platform wall, no public HTTPS endpoint or TLS or
network connector, no real auth-token semantics, no MicroVM image build, no SSM
activation or Session Manager
at all -- and, because a container cannot enforce them, nothing about the agent
sandbox, cgroup ceilings or user namespaces. The crew gateway logs that last one
itself on every start inside a container.
"""
