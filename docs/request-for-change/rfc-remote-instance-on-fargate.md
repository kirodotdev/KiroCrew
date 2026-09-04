---
title: Remote Instance on Fargate
status: draft
kind: framework
author: Raymond Chen (chenmingwei23)
created: 2026-09-07
last-audited: 2026-09-07
audited-at: 424efa423
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Remote Instance on Fargate

## Summary

Add AWS Fargate as a second compute backend for Remote Instances, alongside EC2.
A remote crew becomes a **task** started from a prebuilt image, not a host that
builds its environment at boot, and the owner asks for CPU and memory directly
instead of choosing from three instance shapes.

The case that drives this is **fan-out**. An owner running a conductor locally
wants ten workers without those ten competing for the local machine, and wants
them gone when the run ends. Ten EC2 instances is the wrong unit for that: each
one bootstraps a host to run a container, and the launch cost is paid ten times.

This is not a proposal to change what a remote instance is or how it is reached.
The `LaunchEngine` seam, IAM as the authorisation boundary, and the existing
tunnel and relay surfaces all stay as they are. What changes is what sits at the
other end.

## 1. Problem

### Starting one takes too long, and the reason is structural

An EC2 remote instance builds its environment on every launch. Every claim in this
section was measured at `8fd5193e6`. The `UserData` block of
`src/kiro_crew/cloud/templates/kirocrew-ec2.yaml` does all of this before the
instance is usable:

```
dnf install -y git tmux python3 python3-pip
dnf install -y python3.12 python3.12-pip
dnf install -y ripgrep
curl ... nodejs.org/dist/$NODE_V/$NODE_TARB.tar.gz  → untar
git clone --depth 1 --branch $KirocrewRef $KirocrewRepo
systemctl enable kirocrew.service && systemctl start kirocrew.service
```

Package installs, a Node download over the public internet, and a full repository
clone, none of it cached between launches. On top of that the launcher budgets
`_SIGNIN_ATTEMPTS = 30` in `src/kiro_crew/cloud/launch_engine.py` at roughly five
seconds each for sign-in to complete, and that budget begins only after the
bootstrap has finished.

This is not a tuning problem. The work is real work; it is being done at the wrong
time. An image built once and pulled from ECR does the same work zero times per
launch.

### Sizing is three choices

`src/kiro_crew/cloud/sizes.py` fixes `light` to `instance_type="t4g.xlarge"` and
`balanced` to `instance_type="m7g.2xlarge"`; `power` is the third. A
workload wanting two vCPUs and 8 GB takes the nearest shape and pays for the gap.
Fargate takes CPU and memory as parameters, so the request matches the workload.

### The unit is a host when the work is a container

A remote crew is a process. Giving it an instance means provisioning, bootstrapping
and eventually terminating a host so that one container can run on it. For a single
long-lived instance that overhead amortises. For fan-out it does not.

## 2. Goals

- Launch a remote crew on Fargate through the existing `LaunchEngine` seam, so
  nothing above that seam learns a second backend exists.
- Make the launch cost low enough that starting ten and deleting them is an
  ordinary operation rather than a decision.
- Let the owner ask for CPU and memory rather than choose from three instance
  shapes.
- Keep a Fargate-backed remote crew visible and usable everywhere an EC2-backed
  one is.

## 3. Non-goals

- **Replacing EC2.** A long-lived personal instance with local state is what EC2
  is good at, and this adds a backend rather than migrating one.
- **Any second caller.** Nothing here makes a remote crew reachable by anyone but
  its owner, and no design decision below should be read as leaving room for one.
- **A service, a load balancer, or a scaling policy.** The unit is a task. See
  section 4.
- **Changing how a remote crew is reached, addressed, or relayed.** Those surfaces
  stay as they are.
- **Crew as a service.** Section 10 says what carries over and what does not.

## 4. The case this exists for: fan-out

The concrete shape:

> An owner runs a conductor on their laptop. The conductor needs ten workers. The
> owner does not want ten workers on their laptop's CPU. They want ten remote
> workers to start, do the work, and be deleted.

Read against the current backend, every property of that sentence is a problem.
Ten launches each pay the full bootstrap. Ten hosts are provisioned to run ten
containers. Sizing is picked from three shapes for a workload the owner knows the
exact needs of. Teardown is ten stack deletions.

Read against Fargate, it is the operation Fargate exists for: run N tasks from one
task definition, then stop them.

**The unit here is a task, not a service.** A service is the right shape for
something that must stay reachable and absorb varying load. A fan-out worker is
the opposite: it is started for a known piece of work, it is addressed by whoever
started it, and when the work ends it should not exist. No load balancer, no
desired-count reconciliation, no scaling policy. `RunTask`, then `StopTask`.

Other work that becomes possible for the same reason, once starting a remote crew
is cheap and disposable:

| Use | Why it needs cheap and disposable |
| --- | --- |
| Run a test suite across N remote crews | The point is finishing sooner, so launch cost is the whole game |
| One remote crew per candidate fix | Each exists for one comparison and then never again |
| A crew on a different base image | The image is the parameter, so it costs nothing extra |
| A single tool or agent loop run remotely | Too small to justify provisioning a host |

## 5. Architecture

### What exists

**One image, in ECR.** The crew runtime, its Python environment, Node, and the
tools a crew needs are baked at build time. The image is versioned and referenced
by digest, so what a launch runs is exactly what was published.

**A task definition per crew shape.** It names the image digest, the CPU and
memory the owner asked for, the task role, and the log configuration. Registering
one is an API call, not a deployment.

**A task per remote crew.** `RunTask` starts it, `StopTask` ends it, and nothing
persists between the two except what the crew was told to write elsewhere.

**No service, no load balancer.** Nothing needs to be reconciled to a desired
count, and nothing needs a listener.

### What it plugs into

Provisioning a remote instance already goes through one seam,
the `LaunchEngine` protocol in `src/kiro_crew/cloud/launch_job.py`:

```python
class LaunchEngine(Protocol):
    """The AWS-touching operations a launch needs, injected for testability."""
    def preflight(self, profile, region) -> None: ...
    def provision(self, *, tag, size_key, profile, region) -> str: ...
    def begin_signin(self, *, instance_id, profile, region) -> SigninHandle: ...
    def register(self, *, instance_id, tag, profile, region) -> None: ...
    def teardown(self, *, tag, profile, region) -> bool: ...
```

The EC2 implementation is `RealLaunchEngine` in `src/kiro_crew/cloud/launch_engine.py`,
163 lines with five references to `ec2`, and the engine is already an injectable
field named `cloud_launch_engine` in `src/kiro_crew/dashboard/state.py`, today set
only by tests. A Fargate backend is a second
implementation of these five methods. Everything above the seam, which is the
launch job machinery, the registry, the tunnel manager and the relay surfaces,
does not learn that a second backend exists.

Per method, what changes and what does not:

| Method | Fargate | Versus EC2 |
| --- | --- | --- |
| `preflight` | Credentials, region, image availability | Same shape |
| `provision` | Register a task definition, `RunTask`, return the task identity | Minutes of bootstrap become an image pull |
| `begin_signin` | Sign in inside the task | Same flow, different channel into the container |
| `register` | Into the existing instances registry | Unchanged |
| `teardown` | `StopTask` | Stack deletion becomes an API call |

### Sizing

`size_key` maps to a Fargate CPU and memory pair rather than an instance type.
The existing three keys keep working so nothing above the seam changes, and
because Fargate takes the values directly, adding a key is a table entry rather
than a new instance shape to validate.

## 6. What stays the same

Naming these explicitly, because a new compute backend is a good opportunity to
change things that should not change.

**Authorisation is IAM.** Reaching a remote crew is an authorised call in the
owner's own account. There is no second principal and no new authentication path.

**Credentials live in the CLI's own store on the remote compute.** This is the
property EC2 remote instances already have, and a session that expires is what
makes it acceptable. Fargate does not change the trust level, and nothing here
introduces a long-lived credential.

**One owner.** A remote crew belongs to whoever launched it. Nothing in this RFC
makes a remote crew reachable by anyone else, and no design here should assume a
second caller might appear later.

**The registry, the tunnel, and the relay.** A Fargate-backed remote crew appears
where an EC2-backed one appears. If it does not, this RFC has failed at its main
purpose, which is adding a backend rather than a parallel feature.

## 7. Security considerations

### What changes

**Blast radius moves from an instance role to a task role.** Each task carries
its own role, which is finer-grained than a shared instance profile. This is an
improvement, and it is only an improvement if the role is written per crew rather
than as one role every task assumes.

**A disposable crew is unattended by construction.** It is started to do work and
nobody is watching it, so whatever it is permitted to do it will do without
asking. The permitted set therefore has to be decided when the task is defined,
not during a turn.

**The image becomes a supply-chain surface.** Where EC2 installed packages at
boot from public sources, Fargate bakes them at build time. That is better for
reproducibility and it concentrates trust in the image build. What the image is
built from, and how a launch verifies it is running the digest it asked for, both
need to be answered.

### What does not change

Still one principal, still IAM, still the owner's own account, still credentials
in the CLI's store on remote compute with the same lifetime rules. No inbound
path is added and no data is shared between two parties, because there is only
one party.

## 8. Migration plan

Each phase is independently shippable and independently abandonable. Exit criteria
are written as assertions someone else can check.

### Phase 0: establish the channel into a task

**Blocked on:** the first open question in section 9. Phase 1 cannot commit to a
transport before this is answered, so this phase exists to answer it and nothing
else. No product code.

Exit criteria:

- A Fargate task in a private subnet is reachable from a developer machine, and the
  method is written down with the exact target format and the task-role permissions
  it required.
- If it is not reachable, the verdict says so and names what was tried. A negative
  result ends this phase successfully and redirects phase 1 to an IAM-authorised
  endpoint.
- The verdict is recorded in this document, not only in a pull request.

### Phase 1: the backend

A Fargate implementation of the five `LaunchEngine` methods, a task definition, and
image publication to ECR.

Exit criteria:

- `provision` returns an identity that `register` accepts, and the launched crew
  appears in the instances registry with no change to registry code.
- One remote crew launches on Fargate and serves a turn, reached the same way an
  EC2-backed one is.
- `teardown` leaves no task, no task definition revision in use, and no ECR
  reference held by a stopped task.
- Nothing above `LaunchEngine` branches on backend. Asserted by a test that runs
  the launch job against both engines and compares the resulting registry state.
- A crew with no Fargate configuration still launches on EC2 with byte-identical
  behaviour.

### Phase 2: fan-out

Launch N from one action, address them as a group, tear the group down together.

Exit criteria:

- Launching ten and deleting them is one action each, and a failure to start one
  does not leave the other nine unreachable or unlabelled.
- A partial teardown is recoverable: a second teardown of the same group removes
  what the first one missed, and reports what it removed.
- No orphan survives the owner's process exiting mid-launch. This is the property
  that decides whether fan-out is usable, because ten orphans cost money silently.

### Phase 3: measurement

Exit criteria:

- Published numbers for time to a usable remote crew, Fargate against EC2, on the
  same workload and region.
- Published cost per unit of work for the ten-worker shape, against ten EC2
  instances doing the same work.
- If Fargate is not faster to a usable crew, that number is published too and this
  RFC's central claim is withdrawn rather than restated.

## 9. Backward compatibility

Nothing about EC2-backed remote instances changes. The `LaunchEngine` protocol is
already the seam and already injectable, so adding an implementation does not touch
`RealLaunchEngine`, the launch job machinery, the registry, or the relay.

Existing size keys keep working. `size_key` is the protocol's parameter and the
Fargate backend maps the same three keys to CPU and memory pairs, so a launch that
does not name a backend behaves as it does today.

No stored state changes shape. A Fargate-backed remote crew registers through the
same call with the same fields, so a registry written before this work is readable
after it and the reverse holds too.

## 10. Alternatives considered

**Make the EC2 bootstrap faster.** Bake a custom AMI so packages, Node and the
repository are already present. This removes most of the launch cost without a new
backend. It does not address sizing granularity, it does not make a host the right
unit for a container, and it adds an AMI build and its per-region distribution to
the release process. Worth revisiting if phase 0 finds no workable channel into a
task, because it is the cheapest way to improve the current backend in place.

**A warm pool of EC2 instances.** Keep N started and hand them out. This makes
acquisition fast at the cost of paying for idle capacity and of a pool to operate.
It also does not help the case where each unit of work wants a different image.

**Fargate as a service rather than tasks.** A long-running service per crew,
scaled by policy. This is the right shape when something must stay reachable and
absorb varying load, and it is the wrong shape for a worker that exists for one
known piece of work. It also introduces a load balancer and desired-count
reconciliation for a workload that needs neither.

**Lambda per turn.** Cold start on a turn that loads a crew's skills and tools is
the problem, and a turn is not reliably short enough to fit the execution limit.

**EKS or ECS on EC2.** Both reintroduce capacity the owner has to manage, which is
the thing this RFC is trying to remove.

## 11. Open questions

**How the channel into the task works.** A task can be reached through an
IAM-authorised endpoint, or through a port-forward into the task with no listener
at all. The second is what EC2 remote instances already do, and reusing it is what
would let a Fargate-backed crew appear in the existing relay surfaces without new
plumbing. It needs verifying on Fargate before phase 1 commits to it: the target
format differs from an instance id, and the task role needs messaging permissions
it does not receive by default.

**Sign-in inside a task.** The EC2 flow runs an interactive login on the host and
scrapes the device-code prompt. The equivalent inside a task needs to be
established, and it is the one method of the five with no direct translation.

**Session lifetime against task lifetime.** A disposable task that lives minutes
is a good fit for a short session. A fan-out worker that runs for hours is less
obviously one, and the boundary needs a number rather than a judgement.

**What a task definition is keyed on.** One per crew, one per size, or one per
image digest, and whether definitions are registered on demand or maintained.
This decides whether launching is one API call or two.

**Cold start floor.** An image pull plus crew startup has a floor, and phase 3
should establish it. If the floor is high enough, some fan-out shapes want a warm
pool, which is a different design and not this one.

## 12. Crew as a service, later

The eventual goal of deploying a crew as a service that other people can use
benefits from this work, and this RFC is deliberately not that proposal.

What carries over: the image as the unit of deployment, CPU and memory as
parameters, the task role as the blast radius, and a crew that starts without a
host to bootstrap. A service mode is a long-running Fargate service instead of a
disposable task, which is a change of lifecycle on infrastructure that already
exists rather than new infrastructure.

What does not carry over is the part that matters most. Everything in this RFC
holds because there is exactly one principal, and a crew serving other people
introduces a second one. Caller identity, per-caller data scoping, and audit of
external callers are all out of scope here and none of them should be inferred
from anything above. They are the subject of a separate RFC, and until that
exists, remote instances on Fargate remain single-owner.
