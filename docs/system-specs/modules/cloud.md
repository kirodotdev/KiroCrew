# Cloud Launcher Module

## Overview

`src/kiro_crew/cloud/` runs KiroCrew on the user's **own** AWS EC2 instance with a
single command. It provisions a CloudFormation stack, ships an available local
checkout (or clones the public repo from packaged installs), signs `kiro-cli` in
over SSM, and opens the dashboard through an SSM port-forward. Command surface
(wired in `cli_cloud.py`, invoked via
`kirocrew cloud <action>`):

```
launch | list | status | connect | tunnel | login | stop | start | destroy | iam-policy | iam-boundary | doctor
```

(`iam-boundary` is the one-time admin step that pre-creates the immutable
instance permissions boundary — see the security model below.)

`cloud` verbs are **human/installer actions, never LLM/MCP tools**, guarded in
layers. Be precise about what each layer actually buys, because there is **no
single hard boundary in the default posture**:

- (1) cloud verbs are **not registered as MCP/LLM tools**, so the model is never
  handed them directly;
- (2) the shell `deniedCommands` in `config/defaults.json` (kiro-cli
  `execute_bash`/`shell`) block both the raw AWS CLI verbs (`aws ec2
  terminate-instances` / `delete-*`, `aws cloudformation delete-stack`) **and**
  the `kirocrew cloud destroy|stop|start|launch|connect|tunnel|login` wrappers
  (the latter mint/print tokens); read-only `list`/`status` stay allowed. The
  AWS patterns tolerate global options in BOTH positions — before the service
  AND between the service and the operation
  (`aws(?:\s+--?…)*\s+<service>(?:\s+--?…)*\s+<verb>`) — so neither `aws
  --profile p ec2 terminate-instances` nor `aws ec2 --region r
  terminate-instances` slips past (both bypass forms caught in review).
  The block covers the low-level `s3api` write surface too (`put-object`,
  `copy-object`, the multipart-upload family, `put-bucket-*`), not just the
  high-level `aws s3 cp/mv/sync` — otherwise the launcher's `s3:PutObject` grant
  to `kirocrew-src-*` would be an agent exfiltration path. It also blocks the
  launcher's **creation/mutation** verbs (`cloudformation deploy/create-stack/
  update-stack/*-change-set`, `ec2 run-instances/create-security-group/
  authorize-security-group-*`, `iam create-role/put-role-policy/
  attach-role-policy/create-instance-profile/add-role-to-instance-profile/
  pass-role`), not just the destructive ones — so an agent shell can't provision
  or escalate through the create path either. This is a different layer from
  `security.py`'s underscored `BUILTIN_DENY_PATTERNS`, which don't match
  hyphenated CLI strings;
- (3) an in-layer chokepoint — `run_aws` calls `assert_chokepoint_allowed()`,
  which under an agent session (`KIROCREW_SESSION_KEY` set) allows only an
  **exact** read-only `(service, operation)` allowlist and refuses everything
  else, including secret reads (`secretsmanager get-secret-value` / `ssm
  get-parameter --with-decryption` / `ssm get-command-invocation`); the
  streaming tunnel and mutating ops carry the same `assert_human_action` guard.

**Honest containment model.** Layers (2) and (3) are *best-effort friction*, not
containment: a code-executing agent can obfuscate a shell string,
`del os.environ['KIROCREW_SESSION_KEY']` before an in-process call, or — since
the **default `agent.sandbox = "auto"` resolves to `standard`, which does NOT
hide `~/.aws`** — just run `aws`/boto3 directly. So the load-bearing control is
the **least-privilege IAM scope of the operator's own credentials** (`iam.py` —
tag/ARN-scoped, no IAM writes), and, for operators who want to wall the agent
off from cloud creds entirely, running the agent under the **`strict`/`cc`
sandbox** (which bind-mounts an empty dir over `~/.aws`). The env-keyed guards
deterministically stop honest/accidental misuse and cost nothing, but are not a
claim that a hostile in-process agent is fully contained.

## Module map

| Module | Role |
|--------|------|
| `aws.py` | The single `run_aws` chokepoint — fixed argv, no shell, sandbox-wrapped, `--profile` only (never boto3, never a raw key). `checked`/`checked_json`; `AccessDenied → exact IAM action` mapping; `env_credentials_hint()`. |
| `ec2.py` | `deploy`/`status`/`stop`/`start`/`destroy` via `aws cloudformation` + `ec2`; AZ- **and egress-**aware `discover_network` + `resolve_explicit_subnet` (`--subnet` pin, same guarantees); opt-in `Spot=true` parameter override (`--spot`) plus `find_spot_requests`/`probe_spot_requests` (read-only, for the surface that must ask before it cancels)/`cancel_spot_requests` (cancel, then terminate everything except `exclude_instance_id` — the stack's own instance, which `delete-stack` terminates), which `destroy` runs AFTER the stack lookup and BEFORE `delete-stack` — so a persistent Spot request can't spawn a replacement instance outside the stack, and a failed `DescribeStacks` can't abort a teardown that already terminated the box; `spot_sweep_leaves_live_risk`, which makes `destroy` return `aborted` INSTEAD of deleting a `Spot=true` stack whose request may still be open (the no-stack orphan sweep belongs to whichever surface saw the miss — `cli_cloud` on its "no stack found" early return, the dashboard destroy route on an `already_absent` result); `stack_uses_spot` (the stack's own `Spot` parameter, free off the `describe-stacks` payload) + `grade_spot_sweep`, the one grader both the CLI and the dashboard destroy route report from, and `spot_start_failure_hint` (failure-path only, never raises), which both start surfaces append to a failed `start` so an interruption stop doesn't read as a box worth destroying; tag-based stateless discovery; `_validate_cidr`. `find_stack` verifies BOTH `kirocrew:managed=true` AND `kirocrew:instance==<tag>` before status/stop/start/destroy touch a stack — so a same-prefix managed stack with a different instance tag can't be acted on by the wrong `--tag`. |
| `iam.py` | Least-privilege launcher policy generator (applied by the user, never by KiroCrew) + read-only reachability check + the **content-fixed instance permissions-boundary document** (`boundary_policy_document`/`boundary_arn`) and its constants (`BOUNDARY_NAME`). |
| `ssm.py` | SSM `send-command` run-and-poll (base64-wrapped remote scripts) + `start-session` port-forward; `port_is_free` / `wait_for_local_port`. |
| `login.py` | `kiro-cli` device-code / social sign-in on the box over SSM. |
| `connect.py` | SSM port-forward + token mint + open browser; Instances-registry integration; `redact_token`. |
| `source.py` | Detect and package an editable local checkout (`git archive`, tarfile fallback) and upload it to a per-account S3 bucket; packaged installs instead use the template's public-repo clone fallback. The secret-excluding filter is shared by both packaging paths. Also **`ensure_instance_boundary`** — creates the shared, immutable `kirocrew-ec2-boundary` managed policy once (create-if-not-exists, never re-versioned) and returns its ARN; `delete_instance_boundary` for admin cleanup. |
| `config.py` | Persisted profile / region / tag (**never credentials**); `load()` tolerates a hand-edited/corrupt `cloud.json` — bad JSON *or* a non-object shape falls back to defaults rather than crashing every cloud command. |
| `sizes.py` | arm64/Graviton size tiers (16 GB default `t4g.xlarge`). |
| `ui.py` / `wizard.py` | Terminal UI + the interactive launch flow. `_deploy_with_progress` runs the blocking deploy on a daemon thread and captures the `aws cloudformation deploy` child via a `proc_sink`, so a Ctrl+C on the main (poll) thread terminates it instead of orphaning it (~1800s). An unknown `--size`/`size_key` on the public `launch()` entrypoint yields a clean rc=1 + message, not an uncaught `KeyError`. Resuming a saved stack (`launch` after `stop`) first calls `_ensure_running_and_ssm_ready` — starts a `stopped` instance and waits for SSM `Online` before sign-in/tunnel (which are SSM-only and would otherwise fail); a `terminated` instance fails clean pointing at `--new`. `last_tag` is persisted (`cfg.save()`) **only after** a deploy confirms healthy — a failed first launch leaves no saved pointer, so the next `launch` retries clean instead of resuming a rolled-back/instance-less stack; `_saved_launch_is_usable` additionally ignores a stale saved tag (from an older build) whose stack is in a `_FAILED_STATES` status or has no instance. |
| `templates/kirocrew-ec2.yaml` | The CloudFormation stack. |

## Provisioning shape

CloudFormation stack, one `aws cloudformation deploy` (change-set based), atomic
rollback, one-command `delete-stack` teardown. AMI resolves from the public
`resolve:ssm` Amazon-Linux-2023 alias per arch (no hardcoded AMI ids). A
`WaitCondition` + `cfn-signal` blocks the deploy until the gateway is healthy; a
failed bootstrap folds the on-box setup-log tail into the signal reason so the
cause survives the rollback.

The instance bootstrap runs `install.sh --voice` on both its initial attempt and
retry. This installs the existing `voice` extra (`boto3` and
`amazon-transcribe`) before the gateway first imports its Transcribe provider;
installing those SDKs after startup would otherwise require a gateway restart.

When the installed module belongs to a valid source checkout, the launcher
packages that checkout and uploads it to a launcher-owned bucket
(`kirocrew-src-<account>-<region>`); the instance downloads it with its own IAM
role (`s3:GetObject` scoped to the single object). Wheel and desktop installs
have no checkout to package, so `ec2.deploy` omits `SourceBucket` by default and
the template clones the public repository/ref instead. An explicit
`ship_source=True` remains fail-closed rather than packaging an unrelated
`site-packages` ancestor.

`discover_network` is **egress-kind-aware**, not just "has a default route":
`_subnet_egress_kinds` classifies each subnet's effective route table (explicit
association, else the VPC main table) as NAT (`NatGatewayId`/`NetworkInterfaceId`
default route) or IGW (`igw-` default route). It prefers a **NAT** subnet (works
regardless of a public IP), then an **IGW** subnet — and the launcher threads the
resolved egress kind into the template's `AssociatePublicIp` parameter: **IGW →
`true`** (the Instance `NetworkInterfaces` block attaches a public IP, so an
IGW-routed subnet works even when its `MapPublicIpOnLaunch` is false), **NAT →
`false`** (a private-subnet instance gets NO public IP — it would be unused
surface and can violate SCPs that deny RunInstances-with-public-IP). A subnet
with only a local route (no 0.0.0.0/0 egress) is never chosen — the deploy would
otherwise hang to the `WaitCondition` timeout. An **explicit** route-table
association overrides the main table even when it has no egress: a subnet bound
to a local-only table is treated as no-egress (excluded from the main-table
fallback), so it can't be mistaken for having the main table's egress.

`launch --subnet <subnet-id>` bypasses discovery entirely —
`resolve_explicit_subnet` pins the launch to the given subnet (the only way to
target a dedicated/private-subnet VPC while a default VPC exists, since
discovery always prefers the default VPC). The explicit path keeps discovery's
launch-time guarantees: the subnet must exist in the region, its AZ must offer
the chosen instance type, and it must pass the same `_subnet_egress_kinds`
egress check (NAT or IGW) — each failing fast with actionable text instead of
hanging to the `WaitCondition` timeout, and the same NAT→no-public-IP /
IGW→public-IP parameter wiring applies. `--subnet` applies only to a **new**
stack; reusing an existing stack warns interactively that its network is fixed,
and **hard-fails under `--yes`** — a script's explicitly requested pin must not
be silently ignored.

`launch --spot` opts the new instance into **Spot pricing** (typically 60-90%
below on-demand, varying by tier/AZ/region). It is threaded the same way as
`--subnet` — a single `Spot=true` parameter override, appended **only** when the
flag is set, so an on-demand launch's argv is unchanged and the template's
`Spot: "false"` default stands.

The template expresses it through a **separate `AWS::EC2::LaunchTemplate`
(`SpotLaunchTemplate`, `Condition: IsSpot`)** that the Instance references via
`LaunchTemplate: !If [IsSpot, {...}, !Ref AWS::NoValue]`. That indirection is
forced, not stylistic: `AWS::EC2::Instance` has **no `InstanceMarketOptions`
property** (it is a RunInstances-only parameter — cfn-lint rejects it with
E3002), so a launch template is the only way to put market options on a
CloudFormation single instance. The launch template carries market options and
tag specifications **and nothing else** — AMI, networking, IMDSv2, block devices,
UserData and tags all stay on the Instance, so there is one definition of the box
and the spot/on-demand paths can't drift. With `Spot=false` the launch template
isn't created and the Instance's `LaunchTemplate` resolves to `AWS::NoValue`, so
the on-demand resource graph is byte-for-byte what it was.

The request is pinned to **`SpotInstanceType: persistent` +
`InstanceInterruptionBehavior: stop`**, never AWS's defaults (`one-time` +
`terminate`): the root volume is `DeleteOnTermination: true`, so a
terminate-on-interruption would delete the gp3 root and take the whole data home
(`~/.kiro/crew`) with it. RunInstances only accepts a persistent request with
`stop`/`hibernate`, so the two settings are a package. `ValidUntil` is pinned
explicitly to a far-future date rather than defaulted: the
`LaunchTemplateSpotMarketOptionsRequest` docs give a **7-day default**, expiry
counts as cancellation, and cancelling the request of a *stopped* Spot instance
**auto-terminates** it — which with `DeleteOnTermination: true` is silent data
loss on a box merely parked for a week.

**The launch template's `TagSpecifications` are load-bearing three times over.**
`spot-instances-request` is how `destroy` finds the request to cancel (and what
makes the launcher policy's `aws:ResourceTag` gate on
`CancelSpotInstanceRequests` mean anything; the create side can't be gated, since
AWS documents `aws:RequestTag` on that resource for RunInstances as
unsupported). `instance` and `volume` are what make a **replacement** instance
cleanable: when a persistent request re-opens, the Spot service relaunches from
the *request's* stored launch specification, not from CloudFormation, so the tags
CFN puts on the `Instance` resource never reach that box. Untagged, it is
invisible to the sweep's tag-filtered describe *and* denied by the tag-gated
`ec2:TerminateInstances` — the one orphan the sweep exists for would be the one
it cannot kill. On the primary launch these duplicate the tags CloudFormation
already sends with RunInstances; EC2 merges request-level and template-level tag
specifications (request wins on a duplicate key) and here both values are
identical, so it is a no-op. Pre-existing orphans stay untagged, so the manual
`aws ec2 terminate-instances` fallback remains.

**Honest interruption semantics.** An interruption is *not* fully symmetric with
a user stop. EC2 stops the instance (volume intact) and, because the request is
persistent, restarts it itself when capacity returns. But **only EC2 can restart
an interruption-stopped Spot instance** — `cloud start` on one fails with an AWS
error, and the user has to wait for the auto-resume. A user's own `cloud stop` →
`cloud start` works normally. `cloud status` cannot distinguish the two stop
reasons; the failing `start` is the tell — and it now *says* so rather than
leaving the user to decode a raw AWS error (see `spot_start_failure_hint`
below). Also unhandled: the running agent task
at the moment of reclaim — the 2-minute interruption notice is not observed on
the box, so an in-flight task dies ungracefully, exactly as on a host reboot.

**Teardown owns one resource CloudFormation doesn't.** A persistent Spot request
outlives its instance: terminating a running *or stopped* instance flips the
request back to `open` and EC2 launches a **replacement** outside the stack. So
teardown calls `cancel_spot_requests` (describe by **both**
`tag:kirocrew:managed=true` *and* `tag:kirocrew:instance=<tag>`, then
`cancel-spot-instance-requests`) **before**
`delete-stack`. Ownership takes both keys, exactly as `find_stack` demands them
before status/stop/start/destroy touch a stack: the instance tag alone is a plain
user tag anyone can set, so a foreign request carrying it (or colliding on a
common tag like `dev`) would be cancelled — and its instance terminated — by
`cloud destroy`. The launch template tags our requests with both, and the
tag-gated `ec2:CancelSpotInstanceRequests` grant keys on the managed one, so the
lookup and the policy agree. The state filter is applied **client-side**, excluding the
terminal states (`cancelled`/`closed`/`failed`) rather than allow-listing the
live ones: `disabled` — the state of a persistent request whose instance is
stopped, the one that most needs sweeping — is not a documented value for the
API's `state` filter, and excluding terminal states makes any future state
default to "sweep it". The sweep then **terminates the instances** those requests
point at, because cancelling an `active` request leaves its running instance
alive and a *replacement* instance is not a stack resource that `delete-stack`
would ever touch (for a stopped/`disabled` request EC2 terminates it as part of
the cancel — its doing, not ours, and unavoidable if the request is to go away).

**Except the stack's own instance**, which `destroy` passes as
`cancel_spot_requests(exclude_instance_id=…)` (the id is an output of the
`describe-stacks` payload `find_stack` already returned — no extra call). While
the stack stands, terminating its instance is CloudFormation's job, and doing it
ourselves is not equivalent: if the `delete-stack` that follows is refused
(denied, throttled) we have destroyed the box **and** its `DeleteOnTermination`
root volume out from under a stack that still exists — the user loses
`~/.kiro/crew` and gets nothing they asked for. The orphan path (no stack)
excludes nothing: no `delete-stack` is coming for those.

**Ordering, exactly once.** Inside `destroy` the stack lookup runs **first** and
the sweep second (`find_stack` → sweep → `delete-stack`). The reverse would let a
throttled or denied `cloudformation:DescribeStacks` abort *after* the request was
cancelled and the instance terminated, leaving the user told only "could not
query stack" with no hint the box is already gone. Nothing is lost by waiting:
the orphaned-request case — a rolled-back `--spot` launch whose stack is already
gone — is swept by whichever surface saw the miss: `cli_cloud` on its "no stack
found" early return (the path that skips `ec2.destroy` entirely), the dashboard's
destroy route when `ec2.destroy` comes back `already_absent`. So there is exactly
one sweep per teardown: the caller's when no stack exists, `destroy`'s (after the
lookup, before `delete-stack`) when one does; cancel first, then terminate. This
is why the guide recommends `cloud destroy` after a failed `--spot` launch. For
an on-demand stack it is one describe that finds nothing.

**The orphan sweep asks first.** Cancelling is destructive in a way the verb
hides: a `disabled` request is one whose instance is *stopped*, and EC2
terminates that instance as the request is cancelled — the root volume holding
`~/.kiro/crew` with it. So the CLI's no-stack branch runs the read-only
`ec2.probe_spot_requests` (the lookup half of the sweep, same never-raises
contract and same `error_kind` grading, mutating nothing), prints the request ids
and the instances they point at, and asks the **same confirmation the stack path
asks** — `-y/--yes` skips it, a decline is rc 0 with nothing touched — before
calling `cancel_spot_requests`. Finding nothing keeps the old silent "nothing to
remove" (a prompt for cancelling nothing is a prompt people stop reading), and a
lookup that failed never prompts at all: nothing was mutated, so it goes straight
to the grader. The cancel re-runs the lookup, deliberately: what gets cancelled
is what is live once the user says yes. The dashboard needs no equivalent — its
Delete → "Confirm deleting" UI is answered before the route is called.

`cancel_spot_requests` **never raises** — not `AWSError` and not
`CloudActionDenied` (the CLI's no-stack sweep is the one mutating entry point
with no `assert_human_action` in front of it, so an agent-session `cloud destroy`
on a missing stack must still end in a clean rc-0 "nothing to remove"). It
returns an outcome (`{cancelled, failed, error, error_kind, terminated,
terminate_failed, terminate_error}`) so "nothing to cancel" is distinguishable
from "the cancel was denied". What it looked at is not reported, only what it
did and what it failed to do. `destroy` threads it out as `spot_sweep` — and
only as that, with no happy-path shorthand for the cancelled ids, because a
second spelling of `spot_sweep["cancelled"]` is a key a reader can consult
without ever seeing the failure fields beside it. The CLI **warns with the ids
and the exact runnable `aws ec2 cancel-spot-instance-requests` /
`terminate-instances` command** on a failure, replacing the "You won't be billed
for it" line and **exiting 1** — a
still-open persistent request keeps handing out replacement instances, which is
strictly worse than the "delete did not confirm completion" case that already
exits non-zero so automation can't assume teardown finished.

**A `Spot=true` stack whose sweep failed is NOT deleted.** Deleting is what
creates the zombie: `delete-stack` terminates the instance, the request we could
not cancel (or could not even look up) flips back to `open`, and EC2 launches a
replacement outside a stack that no longer exists — untracked, and billing until
someone finds it. So `destroy` checks `spot_sweep_leaves_live_risk` (the cancel
`failed`, or the lookup returned any `error` — including a *denied* or
agent-refused one, which on a `Spot=true` stack hides exactly the request that
zombies) and returns `{destroyed: False, aborted: True, spot_sweep,
stack_is_spot}` **before** issuing the
delete. Reported, not raised: the sweep outcome *is* the message, and an
exception string cannot carry the ids and remedies. A failed *terminate* is
deliberately not live risk — the cancel succeeded, so nothing can relaunch; that
box is leftover work to report, not a reason to leave the stack standing. Nothing
changes for on-demand stacks (no request, nothing that could relaunch), so the
quiet teardown every old-policy user runs is untouched. Leaving the stack up
costs the instance-hours the user already has, keeps their disk, and keeps
`destroy` re-runnable the moment the request is gone.

Both surfaces then report a refusal, not a teardown. The **CLI** prints the
sweep's remedies, then `Did NOT delete the '<tag>' stack` with why, "cancel the
request(s) — the command above, or the EC2 console — then re-run `kirocrew cloud
destroy`", and "your stack, instance and disk are untouched"; it exits 1 and
**touches no local state** (the registration, the uploaded source and `last_tag`
all still describe a live crew). The **dashboard** answers **409
`spot_sweep_blocked_destroy`** with that message plus the same `warnings` lines,
starts no teardown watcher and sets no `cleanup: "pending"`. The panel's
`deleteMutation.onError` therefore runs instead of `onSuccess`, so no row goes to
"Deleting…" for a stack that is still standing, and `sweepRemediesFromError`
recovers the remedies from `ApiError.body` so the copyable command shows with the
refusal rather than only the sentence naming the problem.

A failed *lookup* is graded by `error_kind` **and by the stack's own `Spot`
parameter**, because the cause alone doesn't decide whether silence is honest.
The authority is the stack, never an inference about the principal: the profile
running `destroy` need not be the one that launched (an admin can create a
`--spot` stack that a restricted profile later tears down), so "this caller
couldn't have made a Spot stack" is not a safe reason to reassure anybody.
`destroy` reads `Spot` off the `describe-stacks` payload `find_stack` already
fetched — no extra API call — and returns it as `stack_is_spot` beside
`spot_sweep`. Then:

* `access_denied` (IAM) or `agent_session` (the chokepoint, which mutated
  nothing) **on a `Spot=true` stack** → sweep failure: a warning naming the
  manual describe, no billing reassurance, rc 1. This is the false-reassurance
  hole the parameter closes.
* the same two **on a `Spot=false` stack** → the old quiet note at rc 0 with the
  reassurance kept, now justified by the stack (it never created a request)
  rather than by guessing at the caller's policy. With **no stack at all** (the
  orphan sweep, on either surface) it stays rc 0 and "nothing to remove", but the
  note says outright that a leftover request can't be ruled out without the
  permission.
* `failed` (throttling, no network, expired SSO, unparseable JSON) → a sweep
  failure on **any** stack, including `Spot=false`: a stack re-deployed without
  `--spot` can still carry a request left by its Spot generation. Warning, no
  reassurance, rc 1, plus the manual `aws ec2 describe-spot-instance-requests
  --filters Name=tag:kirocrew:managed,Values=true
  Name=tag:kirocrew:instance,Values=<tag>` to check by hand — the same two
  filters the sweep itself uses, so the hand-check answers the same question and
  can't surface a foreign request the sweep would never have touched.

The grading itself lives in `ec2.grade_spot_sweep` (`{failed, problems, notes}`),
not in the CLI, because **two** surfaces destroy stacks. `cli_cloud` renders it
to the terminal and exits 1 on `failed`; the dashboard's `DELETE /api/cloud/
{tag}` returns the identical lines — ids plus the runnable `aws` remedy — as
`warnings`: on its 200 when the delete *was* accepted (this is work left over),
audited as `partial` rather than `success`, or on the 409 above when the delete
was refused. It adds nothing at all when the sweep is clean. A stack destroyed from the panel leaks exactly as much money as
one destroyed from a terminal, so the two must not drift.

Within one problem the **detail order is part of the contract**: the raw AWS
error first, the runnable `aws` remedy **always last**. The terminal then ends on
the line the user can act on, and the dashboard — which flattens
`summary + details` into one string — can peel the trailing command back off it
and render it as selectable, copyable `<code>` instead of prose that wraps
mid-flag. A remedy followed by more prose would drag that prose into the code
block, and a mistyped `--spot-instance-request-ids` leaves the request live. The
panel splits on the `aws ec2 ` marker and gives `notices` the neutral note
treatment rather than the amber warning block: "nothing proves it either way" and
"this is still billing" are different claims, and dressing the first as the
second is how the second stops being read.

**A failed `cloud start` on a `--spot` stack explains itself.** The docs above
call the failing start "the tell" for an interruption stop; the product now says
so at the moment it happens, on both surfaces. `ec2.spot_start_failure_hint`
reads the stack's `Spot` parameter and, when it is true, returns the lines the
CLI prints under `ui.fail` and the dashboard appends to the 502's `error` (the
field its client unwraps, behind a single newline the panel splits on — a
structural seam, so the hint stays free to be reworded or translated): likely an
interruption stop, only EC2 can restart it,
it resumes on its own, your data is intact, **do not destroy the instance to fix
it**. That last line is the point — the obvious reaction to "start is broken" is
delete-and-relaunch, and destroy takes the `DeleteOnTermination` root volume the
interruption deliberately preserved. The lookup is a **failure-path** call: a
successful start makes exactly the AWS calls it always did, and the hint never
raises, so a second failure inside it cannot displace the error the user needs.

Like `--subnet`, `--spot` only takes effect on a **new** stack — a resumed stack
keeps the pricing model it was created with (warn interactively, hard-fail under
`--yes`), and moving an existing box onto Spot means launching a new one
(`--new --spot`). Spot compounds with, rather than replaces, an EventBridge
scheduled stop/start: it cuts the instance-hour rate while running, the schedule
cuts the hours, and neither touches the NAT gateway floor.

The optional SSH CIDR is also **normalized** (host bits cleared, `1.2.3.4/24` →
`1.2.3.0/24`) so the SG ingress rule is canonical. `get_stack_failures` sorts the
specific bootstrap reason ahead of CloudFormation's generic `[WaitCondition]`
cascade lines (events are newest-first, so the generic line would otherwise bury
the root cause), and drops the generic noise entirely once a specific reason
exists.

Teardown removes the uploaded source object as part of the contract: after a
**confirmed** `delete-stack`, `source.delete_source` returns
`{removed, uri, error}` and the CLI warns with the exact `aws s3 rm <uri>`
command if it couldn't be deleted (a non-zero `s3 rm` is a real failure — denied,
wrong bucket — since `s3 rm` succeeds silently on an already-absent key) rather
than silently leaving a private tarball billing. If the stack delete itself did
not confirm, the source object and `last_tag` pointer are preserved and the CLI
exits non-zero.

## Security model

- **No stored credentials.** `cloud.json` holds profile name + region + tag only;
  the `aws` CLI resolves credentials via its own provider chain. Env-var-only
  credentials are unsupported (the sandbox scrubs `AWS_SECRET*`/`AWS_SESSION*`);
  `env_credentials_hint()` detects that and prints an actionable message.
- **Injection closed in depth.** tag/region/profile/CIDR/repo/ref/run_as are
  charset-validated (`validation.FieldSpec`) before reaching argv, **and** the
  template mirrors those charsets as `AllowedPattern`s so a direct
  `aws cloudformation deploy` can't inject shell metacharacters into the root
  UserData. SSM remote scripts are base64-wrapped so `AWS-RunShellScript` can't
  mangle them. `${!tail_ctx}` in the template is a `!Sub` literal escape, not a
  bug (guarded by a test).
- **IMDSv2 enforced** on the instance (`HttpTokens: required`, hop limit 1):
  KiroCrew runs a prompt-injectable agent, so an SSRF/injection that reaches the
  metadata endpoint must not read the instance role's STS credentials via IMDSv1.
- **No unverified remote scripts in bootstrap.** Node.js installs from the AL2023
  AppStream `dnf` repo (the reliable primary path, validated live). The
  NodeSource fallback does NOT `curl … | bash` a remote installer; it imports
  NodeSource's GPG key over pinned TLS and writes a `gpgcheck=1` dnf repo, so RPM
  signatures are verified before install. kiro-cli is fetched over
  `--proto '=https' --tlsv1.2` and the binary presence is asserted afterward
  (fail-closed WaitCondition on a broken install).
- **Least-privilege, tag-/prefix-scoped IAM.** The RCE-adjacent SSM verbs
  (`ssm:StartSession` / `ssm:SendCommand` on instances) and the EC2 destructive
  verbs (`DeleteSecurityGroup` / `RevokeSecurityGroupIngress` / `DeleteTags`)
  require `kirocrew:managed=true`; CloudFormation stack mutation/delete is scoped
  to `stack/kirocrew-*/*`, and the change-set verbs (which `aws cloudformation
  deploy` authorizes on the **changeSet ARN**, not just the stack ARN) are a
  separate statement scoped to both `changeSet/kirocrew-*/*` and
  `stack/kirocrew-*/*` (scoping to `stack/*` alone would deny the launch under
  the generated policy); only enumerate/`GetTemplateSummary` stay on `*`.
  `iam:PassRole` is scoped to `kirocrew-ec2-*`; S3 is scoped to `kirocrew-src-*`.
  Command-history
  read is minimal: `ssm:GetCommandInvocation` (needed to poll `send-command`
  results) is granted but `ssm:ListCommandInvocations` is NOT, so a leaked
  launcher credential can't blindly enumerate the command output that carries the
  minted dashboard token.
- **Create verbs require the managed request-tag (scoped to the CREATED ARNs).**
  `Ec2CreateTaggedResources` requires `aws:RequestTag/kirocrew:managed=true` for
  `ec2:RunInstances` on `instance/*`, `ec2:CreateSecurityGroup` on
  `security-group/*` and `ec2:CreateLaunchTemplate` on `launch-template/*` — so a
  leaked launcher credential can't create untagged twins that sit outside the
  tag-gated Stop/Terminate/Delete/Authorize statements. Because these calls
  authorize **per-resource** across ARNs that don't carry the tag (RunInstances
  also creates an untagged volume + ENI and references an existing
  image/subnet/SG/launch-template), a blanket request-tag 403s the launch — so
  those ARNs are granted unconditioned in `Ec2RunInstancesSupportingResources` /
  `Ec2CreateSecurityGroupVpc`. Proven with a least-privilege assumed-role
  `run-instances --dry-run` (tagged instance ALLOWED incl. the template-shaped
  call, untagged DENIED; tagged SG ALLOWED, untagged DENIED).
  `spot-instances-request/*` is deliberately in the **unconditioned** statement:
  AWS's own IAM example doc (`ExamplePolicies_EC2.html#iam-example-spot-instances`)
  documents `aws:RequestTag` on that resource for `RunInstances` as **not
  supported**, and the gate would be theatre regardless — IAM only evaluates an
  ARN the request actually names, so an untagged request (no spot-request
  `TagSpecification`) skips the resource entirely and the condition never fires.
  A policy cannot prevent a rogue *untagged* Spot request; what it can do is
  ensure ours are always tagged (the launch template's `TagSpecifications`),
  which is what makes the `aws:ResourceTag` gate on `CancelSpotInstanceRequests`
  meaningful.
- **Mutating verbs require the managed resource-tag.**
  `Ec2ManagedResourceMutateTagged` gates every verb that touches an *existing*
  EC2 resource — Authorize/Revoke SG rules, DeleteSecurityGroup, DeleteTags,
  Stop/Start/Terminate/Reboot, and (for `--spot`) `DeleteLaunchTemplate` +
  `CancelSpotInstanceRequests` — on `aws:ResourceTag/kirocrew:managed=true`.
- **Spot additions (`--spot`).** Beyond the create/mutate gates above:
  `ec2:DescribeLaunchTemplates`/`DescribeLaunchTemplateVersions`/
  `DescribeSpotInstanceRequests` join the read-only `Ec2Discovery` statement, and
  `IamCreateSpotServiceLinkedRole` grants `iam:CreateServiceLinkedRole` pinned
  BOTH by resource path (`role/aws-service-role/spot.amazonaws.com/
  AWSServiceRoleForEC2Spot*`) and by `iam:AWSServiceName=spot.amazonaws.com`. That
  role is required for the first Spot request in an account; the console creates
  it silently but the CLI does not. A service-linked role can only be assumed by
  its own service and its permissions are fixed by AWS, so creating it grants the
  caller nothing.
- **The policy is at the IAM size ceiling.** A customer managed policy is capped
  at **6,144 characters** (whitespace excluded) and the cap cannot be raised. The
  generated policy is ~6.0k, so it fits with little headroom — which is why the
  request-tag statements are one `Ec2CreateTaggedResources` and the resource-tag
  statements one `Ec2ManagedResourceMutateTagged` rather than the six they were
  before. Both merges are safe (see the module comments): the resource-tag merge
  is *exactly* equivalent — identical `Resource: "*"` and identical condition —
  and the create merge's action×resource cross-product is **inert given current
  EC2 authorization semantics**: `CreateSecurityGroup` is never evaluated against
  an instance/launch-template ARN and `CreateLaunchTemplate` never against an
  instance/SG ARN, while the one *live* cross-product — `RunInstances` on
  `security-group/*` and `launch-template/*`, which it genuinely does authorize
  against (that is why `Ec2RunInstancesSupportingResources` exists) — is a
  **strictly narrower duplicate** of the unconditioned grant in that statement.
  `test_policy_fits_iam_managed_policy_limit` pins this; the next addition should
  split the printed policy in two rather than loosen a gate.
- **Escalation primitives constrained + immutable, pre-created permissions
  boundary.** `ec2:CreateTags` is gated by an `ec2:CreateAction` condition
  (`RunInstances`/`CreateSecurityGroup`/`CreateLaunchTemplate`) so it can only tag
  at creation — a holder can't tag an *existing* resource `kirocrew:managed=true`
  to pull it under the tag-gated Stop/Terminate/Delete statements. `iam:AttachRolePolicy`/`DetachRolePolicy`
  are pinned by `iam:PolicyARN` to exactly `AmazonSSMManagedInstanceCore`. The
  `PutRolePolicy` escalation is closed by a **required permissions boundary** that
  is now **shared, content-fixed, and immutable** — closing the earlier
  self-authorship gap:
  - The boundary is a **single** managed policy named `kirocrew-ec2-boundary`
    (NO per-`StackTag` suffix), created **once** by launcher CODE
    (`source.ensure_instance_boundary`, via the `aws.run_aws` chokepoint) —
    **not** per-launch CloudFormation. It is create-if-not-exists (tolerates
    `EntityAlreadyExists`) and NEVER re-versioned. Its content = the exact
    `AmazonSSMManagedInstanceCore` action set + `s3:GetObject` on
    `kirocrew-src-<account>-*/*` (region-agnostic — IAM is global; the
    whole-prefix read is safe because a boundary only *caps*, and the role's
    INLINE `SourceObjectRead` policy still pins the actual read to the single
    derived object).
  - The template no longer creates the boundary; the `InstanceRole` references it
    by a FIXED ARN via a new `PermissionsBoundaryArn` parameter (AllowedPattern
    `^arn:aws:iam::[0-9]{12}:policy/kirocrew-ec2-boundary$`), which the launcher
    fills with `arn:aws:iam::<account>:policy/kirocrew-ec2-boundary`.
  - The launcher policy grants only `iam:CreatePolicy` + `iam:GetPolicy` on that
    **exact** ARN (`IamInstanceBoundaryCreateOnce`) — and NO
    `CreatePolicyVersion`/`DeletePolicyVersion`/`DeletePolicy`. This is the crux:
    `CreatePolicy` on a fixed name fails `EntityAlreadyExists` once the boundary
    exists, and with no version/delete verb a **leaked launcher credential cannot
    make an existing boundary permissive**. So the ceiling holds not just against
    the prompt-injectable on-box agent but against a leaked *launcher* credential.
  - `iam:CreateRole` remains gated on `ArnLike iam:PermissionsBoundary ==
    arn:…:policy/kirocrew-ec2-boundary` (`ArnLike`, NOT `StringEquals` — the
    latter would deny CreateRole under the generated policy; verified with the
    IAM policy simulator). `PutRolePolicy` is a separate role-ARN-scoped statement
    — a boundary set at CreateRole can't be removed by it.
  - **Residual (first-write race), tracked in as-built:** the very first
    `CreatePolicy` could be run by an attacker holding the launcher policy BEFORE
    the legitimate first launch, seeding a permissive boundary at that name. That
    is materially smaller than the old "author an arbitrary boundary at any time"
    hole. Operators who want it gone entirely run `kirocrew cloud iam-boundary`
    once as an admin, then drop the `IamInstanceBoundaryCreateOnce` statement from
    the applied launcher policy (the launcher then only *references* the ARN, with
    no `CreatePolicy` grant). The agent-shell deny-list also blocks
    `aws iam create-policy`/`create-policy-version`.
  The instance role's inline `s3:GetObject` is still pinned to the **derived**
  launcher path (`kirocrew-src-${AccountId}-${Region}/${StackTag}/…`), not the
  `SourceBucket`/`SourceKey` deploy params — so a caller can't grant the box read
  on an arbitrary S3 object. `ec2:AuthorizeSecurityGroup{Ingress,Egress}` are
  tag-gated (`aws:ResourceTag/kirocrew:managed=true`) so a leaked credential
  can't open ingress on an unrelated security group.
- **`PutRolePolicy`/`PassRole` tag-gated (not just name-prefix).** Both
  `iam:PutRolePolicy` and `iam:PassRole` (to EC2) additionally require
  `aws:ResourceTag/kirocrew:managed=true` on the target role — not just the
  `kirocrew-ec2-*` name prefix. Without the tag gate, a leaked launcher credential
  could target a **pre-existing** `kirocrew-ec2-*` role that a third party created
  out-of-band **without** our permissions boundary (so `CreateRole`'s boundary gate
  never applied), inline an admin policy, and pass it to EC2. The tag makes the
  constraint non-spoofable: only a role WE created via the boundary-gated
  `CreateRole` — which applies `Tags` **atomically** at creation (see the
  template's `InstanceRole.Tags`) — carries `kirocrew:managed=true`, and the tag
  lands in the same call, so there is no untagged window before CFN's subsequent
  `PutRolePolicy`. `aws:ResourceTag` (the global key, honored by both actions —
  verified with the IAM policy simulator: allowed with the tag, `implicitDeny`
  without it; and live: role created + tagged + inline-policy'd + SSM Online). No
  `iam:PermissionsBoundary` condition is added to `PutRolePolicy` (that key isn't
  in its request context; it would deny the call).
- **`iam:TagRole` gated so the tag gate can't be self-defeated.** The tag gate
  above is only non-spoofable if the *same* launcher policy can't apply the tag
  to an arbitrary role — otherwise a leaked credential could tag a pre-existing
  unbounded `kirocrew-ec2-*` role `kirocrew:managed=true`, then inline admin +
  pass it. `iam:TagRole` is therefore **not** unconditioned in the role-management
  statement; it is its own statement gated on
  `aws:ResourceTag/kirocrew:managed=true` (`IamTagRoleOnManaged`). `TagRole` is
  still *required* because CloudFormation's `CreateRole` passes the role's `Tags`
  inline and AWS authorizes that as `iam:TagRole` (`id_tags_roles.html`). The
  gate works because of an empirically-verified asymmetry (least-privilege
  assumed-role harness): at `CreateRole`, AWS evaluates the embedded `TagRole`
  authorization with `aws:ResourceTag` reflecting the tags **being applied**, so
  the boundary-gated create is **ALLOWED**; a **standalone** `tag-role` on an
  unmanaged pre-existing role finds the key absent and is **DENIED**. So the
  launcher can tag a role it is creating (already carrying the tag in context) but
  cannot add the managed tag to a role that lacks it — closing the full chain
  (`TagRole`→`PutRolePolicy`→`PassRole`) at the first step. NB: a boundary
  (`iam:PermissionsBoundary`) condition does **not** work here — AWS does not
  propagate that key into the `CreateRole`-embedded `TagRole` check (it denied the
  legitimate create in the harness); `aws:ResourceTag` is the key that works.
- **Anti-squat bucket pin, end to end.** The launcher source bucket name is
  deterministic (`kirocrew-src-<account>-<region>`) and thus globally
  guessable/squattable. Every S3 op that could ship or reveal source pins
  `--expected-bucket-owner <account>`: `head-bucket`/`create-bucket`
  (`ensure_bucket`) AND the upload/delete — so a delete-and-recreate race between
  the check and the upload can't land the tarball in a stranger's bucket (S3
  returns 403, we fail closed). Upload/delete use the low-level `s3api
  put-object`/`delete-object` because only s3api accepts
  `--expected-bucket-owner`; the high-level `aws s3 cp`/`rm` reject it as an
  unknown option (caught in live-deploy testing). The owner value for
  upload/delete is **derived from the (fail-closed-resolved) bucket name**
  (`_account_from_bucket`), NOT a second `sts:get-caller-identity` — a transient
  STS `""` would otherwise silently DROP the pin and ship/delete without owner
  verification; if the account can't be derived (the `kirocrew-src-unknown-*`
  fallback), upload raises and delete returns `removed=False` rather than issue an
  unpinned call. The public-access block is
  enforced (fail-closed) on **every** `ensure_bucket` path — freshly-created AND
  reused — not just on create, so a pre-existing `kirocrew-src-*` bucket whose
  BPA was disabled can't silently receive private source (the call is idempotent).
- **SSM-only by default** — no inbound ports, no SSH key; the gateway binds
  loopback and is reached via tunnel + minted token. The dashboard token transits
  `send-command` output (retained in SSM history); accepted trade-off, mitigated
  by short TTL, loopback-only use (needs a tunnel = `StartSession`), the
  `ListCommandInvocations`-denied grant above, and the agent-session chokepoint
  denying `GetCommandInvocation`. `connect()` mints the token **only after** the
  tunnel is confirmed ready (`wait_for_local_port` + the ownership recheck), so a
  failed connect attempt never leaves an unused token sitting in SSM history for
  its TTL. If the mint then fails on a ready tunnel, `connect()` tears the tunnel
  down (`_terminate`) and returns `ready=False` rather than a ready-but-URL-less
  connection that would orphan the SSM child or hang the wizard.
  The on-box `kiro-cli login` log + FIFO under `/tmp` (device-code
  URL/code, OAuth callback) are created with `umask 077` (0600) so a second local
  user can't read them from world-readable `/tmp`. The optional `AllowSshCidr` is refused wider
  than /16 (use your own IP/32). EBS is encrypted; the source bucket blocks
  public access.
- **Port-forward safety.** `connect()` and `login`'s callback refuse if the local
  port is already occupied (`port_is_free`) and pass `proc=` to
  `wait_for_local_port`, so a dashboard token / OAuth code can never be routed to
  a foreign local listener. A final ownership recheck closes the residual
  free-check→bind race: because only one process can bind the port, a listener
  answering while our SSM child has already exited is a foreign process that won
  the bind — so both paths refuse in that case rather than send the token/code.
  Teardown kills the whole **process tree** (`killpg`, since `open_port_forward`
  uses `start_new_session=True`): `proc.terminate()` alone would signal only the
  `aws` wrapper and leave the `session-manager-plugin` child — which actually
  holds the forwarded port — alive after Ctrl+C or a mint failure. The shared
  `ssm.kill_port_forward` does the tree teardown, and **both** the dashboard
  tunnel (`connect.Connection.close`/`_terminate`) and the login callback tunnel
  (`login._close_process`) go through it, so neither leaves an orphaned
  plugin/port. **Windows** has no process groups (`start_new_session` is silently
  ignored and `os.killpg`/`os.getpgid` do not exist), so it reaps the tree with
  `taskkill /T /F` and falls through to the single-proc path only if that is
  unavailable. Both platforms escalate SIGTERM→SIGKILL when the graceful stop does
  not reap; signal numbers come from `platform_compat` (**never** `signal.SIGKILL`,
  which is undefined on Windows — naming it inside the escalation's own
  `except Exception` swallows the `AttributeError` and skips `proc.kill()`
  entirely, on the very platform that reaches that path). Every
  no-URL exit reaps its tunnel too: `connect()` folds a mint failure — whether
  `mint_token` returns `""` **or raises** — into a `ready=False` Connection after
  `_terminate`; and the **social-login** paths close their callback tunnel on the
  url-less branch — `start_device_login` reaps `proc` in-function when the
  continued step yields no URL (leaving `port_forward` unset rather than handing
  back a live-tunnel-but-url-less prompt), and `wizard._verify_operational` calls
  `prompt.close()` on its no-URL return (mirroring the `launch()` branch) so no
  social-login path orphans a loopback callback port.
- **Secret hygiene in source shipping.** Both packaging paths ship only
  **tracked** files: `git archive` (which honors `.gitignore`), and the fallback
  builds from `git ls-files` — so an untracked/gitignored secret with an
  arbitrary name (`secrets.yaml`, `.envrc`, `local_settings.py`) is never
  packaged; the fallback **fails closed** if the tracked-file list is
  unavailable rather than walk the whole tree. `git archive` packages the
  *committed* tree, so when the tracked working tree is **dirty** (uncommitted
  edits) `build_source_tarball` switches to the `git ls-files` working-tree path
  — otherwise `cloud launch` would silently ship stale last-commit code. The fallback also adds each entry
  **non-recursively** and skips gitlink directories: `git ls-files` lists a
  submodule as a single directory entry, and a recursive `tar.add` on it would
  package the submodule's untracked/gitignored files — so we never let tar walk
  a directory for us. On top of that, both paths run the denylist (`.kirocrew*`,
  `.aws`, `.ssh`, `.gnupg`, `.env*`, `*.pem`/`*.key`/`*.p12`, credential
  filenames) and drop a custom-named `KIROCREW_HOME` (incl. nested) under the
  repo. `redact_token()` strips JWTs before any log line.

## Bootstrappers

`install.ps1` (Windows client) and `cloud-install.sh` (macOS/Linux) ensure the
`aws` CLI + `session-manager-plugin` + Python are present, then hand off to
`kirocrew cloud launch`. They install *client* prerequisites only — the gateway
always runs on the Linux EC2 box, never on Windows.
`cloud-install.sh --voice` additionally installs the existing `voice` extra in
the launcher's managed client venv; the EC2 bootstrap includes that extra by
default regardless of this client-side flag.

`kirocrew cloud launch` runs `python -m kiro_crew`, which imports the whole CLI —
including gateway/cron/session modules (plus `apps/bridges` and the PTY
`dashboard/handlers/terminal`) that use POSIX `fcntl` (`flock` for advisory
locks, `ioctl` for PTY control). **All** such modules on the CLI import path
import `fcntl` through `flock_compat` (a shim that delegates to real `fcntl` on
macOS/Linux; on Windows `flock`/`LOCK_*` no-op and `ioctl` raises), letting the
Windows *client* path import the CLI and reach the cloud launcher without a
`ModuleNotFoundError: fcntl`. This is safe because the gateway/cron/PTY code that
actually locks or drives a terminal never runs on Windows — the client only
provisions a remote Linux box and exits. (Guarded by a simulated-no-`fcntl`
import test so a future bare `import fcntl` on the CLI path is caught.)

## Tests

`test/test_cloud_{aws,ec2,iam,ssm,login,connect,source,config,sizes,ui,wizard,cli}.py`
plus `test_update_git_guard.py`. AWS I/O is mocked at the `cloud.aws` chokepoint;
`kiro-cli` is never spawned for real.
