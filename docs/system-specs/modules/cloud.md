# Cloud Launcher Module

## Overview

`src/kiro_crew/cloud/` runs KiroCrew on the user's **own** AWS EC2 instance with a
single command. It provisions a CloudFormation stack, ships an available local
checkout (or clones the public repo from packaged installs), signs `kiro-cli` in
over SSM, and opens the dashboard through an SSM port-forward. Command surface
(wired in `cli_cloud.py`, invoked via
`kirocrew cloud <action>`):

```
launch | list | status | connect | tunnel | login | logout | stop | start | destroy | iam-policy | iam-boundary | doctor
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
  the `kirocrew cloud destroy|stop|start|launch|connect|tunnel|login|logout` wrappers
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
| `aws.py` | The `run_aws` chokepoint for captured AWS CLI calls — fixed argv, no shell, sandbox-wrapped, `--profile` only (never boto3, never a raw key). `checked`/`checked_json`; `AccessDenied → exact IAM action` mapping; `env_credentials_hint()`. |
| `ec2.py` | `deploy`/`status`/`stop`/`start`/`destroy` via `aws cloudformation` + `ec2`; AZ- **and egress-**aware `discover_network` + `resolve_explicit_subnet` (`--subnet` pin, same guarantees); tag-based stateless discovery; `_validate_cidr`. `find_stack` verifies BOTH `kirocrew:managed=true` AND `kirocrew:instance==<tag>` before status/stop/start/destroy touch a stack — so a same-prefix managed stack with a different instance tag can't be acted on by the wrong `--tag`. |
| `iam.py` | Least-privilege launcher policy generator (applied by the user, never by KiroCrew) + read-only reachability check + the **content-fixed instance permissions-boundary document** (`boundary_policy_document`/`boundary_arn`) and its constants (`BOUNDARY_NAME`). |
| `ssm.py` | SSM `send-command` run-and-poll (base64-wrapped remote scripts) + `start-session` port-forward; `open_port_forward()` directly spawns the streaming `aws ssm start-session` child because `run_aws` captures output, and calls `aws.assert_human_action()` before doing so; `port_is_free` / `wait_for_local_port`. |
| `login.py` | `kiro-cli` device-code / social sign-in on the box over SSM, plus `cancel_device_login` — which stops a login this crew started and removes the files holding its code, WITHOUT dropping the box's session, because a cancelled attempt must not sign the crew in later and must not take an older valid session with it — and `logout` — the account switch. `login` short-circuits on an existing session, so `logout` is what makes a different Kiro account reachable without a hand-run SSM command. It kills any still-polling background `kiro-cli login` **and** any live `kiro-cli acp` runtime **before** signing out (otherwise the login re-authenticates the old account, and an ACP runtime keeps serving the old account's in-memory credential until its next 401), removes the login log/PID/FIFO (they hold the previous device-code URL + code, which must never be re-shown as a fresh prompt), and confirms the result with `is_logged_in` rather than the exit code — `kiro-cli logout` exits non-zero when there was no session to drop, which is still the requested state. That confirmation fails CLOSED: it requires a positive signed-out sentinel (`__NOAUTH__`), so an SSM timeout or transport error — where the session may still be active — reports failure rather than a false "signed out". The same fail-closed applies to the cleanup command itself: if that SSM invocation doesn't return `Success`, the kills it was meant to do can't be trusted and logout reports failure without probing. The CLI warns the operator that in-flight chats/cron sessions are stopped (their runtimes are killed). |
| `connect.py` | SSM port-forward + token mint + open browser; Instances-registry integration; `redact_token`. **`connect_fargate`** is the Fargate lane's counterpart and mints NOTHING: a Fargate task runs the crew container, whose only listener is a front proxy serving a JSON turn API with the backend loopback-only and every control path authorisation-gated and then 404, so there is no mint route to call and no browser to open. It preflights the task's execute-command channel through `ssm.task_exec_readiness`, opens the forward through the shared `ssm.open_port_forward` (which carries the human-action gate and the process-group teardown), and returns the local base URL and turn path on a `FargateConnection`. `is_launched_instance()` prevents the generic instance PATCH endpoint from rewriting a correlated launch’s connection method, SSM target, AWS profile, or region, so Stop/Start/Delete retain the stack address and a running billable instance is not stranded. |
| `source.py` | Detect and package an editable local checkout (`git archive`, tarfile fallback) and upload it to a per-account S3 bucket; packaged installs instead use the template's public-repo clone fallback. The secret-excluding filter is shared by both packaging paths. Also **`ensure_instance_boundary`** — creates the shared, immutable `kirocrew-ec2-boundary` managed policy once (create-if-not-exists, never re-versioned) and returns its ARN; `delete_instance_boundary` for admin cleanup. **`ensure_crew_boundary`** and **`ensure_crew_exec_boundary`** do the same for the two Fargate ceilings (`kirocrew-crew-boundary`, `kirocrew-crew-exec-boundary`), and all three route through one `_ensure_boundary` sequence whose ORDER is the security property: an existing policy is verified against the expected content-fixed document BEFORE it is reused, and a lost create race is verified on the way back, so a permissive policy seeded at either name is refused rather than trusted to cap nothing. |
| `config.py` | Persisted profile / region / tag **plus the optional `fargate` block** (**never credentials**); `load()` tolerates a hand-edited/corrupt `cloud.json` -- bad JSON *or* a non-object shape falls back to defaults rather than crashing every cloud command. The `fargate` field holds the block **exactly as read**, and `fargate_config()` is what judges it. **This module has no writer:** no `save()`, no `apply_update()`, no lock. `profile` / `region` / `last_tag` are still READ here so an install whose pointer predates `launch_state.py` keeps resuming, and `launch_state.py` is where those three are written now. See "The Fargate lane's configuration home" and "Where launch state lives" below. |
| `launch_state.py` | The product-owned launch record (`cloud_launch_state.json`): the profile, region and tag a LAUNCH decided. One writer, three fields, frozen dataclass, whole-record `atomic_write`. `load()` falls back to the legacy fields in `cloud.json` when the record holds none, read-only, so `cloud resume` works on an install that predates it. `clear_tag(expect)` clears only while the pointer still names the stack `destroy` deleted. See "Where launch state lives" below. |
| `sizes.py` | arm64/Graviton size tiers (16 GB default `t4g.xlarge`). |
| `fargate/` | A crew's Fargate task definition and `RunTask` request produced as **data**, by pure functions that call nothing. `identity.py` recovers which crew an ARN belongs to and refuses a document naming more than one; `taskdef.py` holds the revision key and the `RegisterTaskDefinition` document; `runtask.py` holds the launch request and the closed set of overrides it may carry. See "Fargate task definitions as data" below. |
| `fargate_engine.py` | The `LaunchEngine` for Fargate, reachable through `engine_for("aws_fargate")` when -- and only when -- `cloud.json` carries a complete `fargate` block (see the `config.py` row and "The Fargate lane's configuration home" below). The lane is registered by `DefaultRemoteProvisionerProvider`, which appends the descriptor to `provisioners()` on the same condition; an absent or incomplete block leaves the lane unregistered rather than registered and refusing, and `engine_for` then raises the same `KeyError` an unknown id raises. Registration makes the lane reachable through the API and does **not** put a row in the Set-up picker: `kind` names a frontend form and no renderer claims this kind yet, so the dashboard skips it. Its teardown decides ownership from a task's tags: the `kirocrew:managed` marker is checked as a boolean gate **before** the identifier, so a task whose marker holds anything other than `true` is `UNMARKED` and is never stopped, and identity is then read under the imported `LAUNCH_TAG_KEY`. A running task this launch started but whose launch tag is absent or names another launch is `MISLABELLED`; both it and a running `UNMARKED` task carrying this launch's `startedBy` are REFUSED rather than deleted, and the plan returns `confirmed=False` naming the ARNs, because deleting on a `startedBy` match alone is a guess and reporting `True` would tell the operator their billing stopped when a task may still be running. Neither the marker's key nor its value nor the launch-tag key is spelled here; all three are imported from the modules that own them. |
| `ui.py` / `wizard.py` | Terminal UI + the interactive launch flow. `_deploy_with_progress` runs the blocking deploy on a daemon thread and captures the `aws cloudformation deploy` child via a `proc_sink`, so a Ctrl+C on the main (poll) thread terminates it instead of orphaning it (~1800s). An unknown `--size`/`size_key` on the public `launch()` entrypoint yields a clean rc=1 + message, not an uncaught `KeyError`. Resuming a saved stack (`launch` after `stop`) first calls `_ensure_running_and_ssm_ready` — starts a `stopped` instance and waits for SSM `Online` before sign-in/tunnel (which are SSM-only and would otherwise fail); a `terminated` instance fails clean pointing at `--new`. `last_tag` is persisted (to the launch record, via `_record_launch`) **only after** a deploy confirms healthy, and a write that fails there WARNS rather than aborting -- the instance is already billing and sign-in still has to happen — a failed first launch leaves no saved pointer, so the next `launch` retries clean instead of resuming a rolled-back/instance-less stack; `_saved_launch_is_usable` additionally ignores a stale saved tag (from an older build) whose stack is in a `_FAILED_STATES` status or has no instance. |
| `templates/kirocrew-ec2.yaml` | The CloudFormation stack. |
| `templates/kirocrew-fargate-base.yaml` | The account-and-region Fargate stack: the ECS cluster and an egress-only security group with no ingress. One per account and region, shared by every crew's tasks, so deleting one crew's stack cannot delete the cluster its siblings run on. |
| `templates/kirocrew-fargate-crew.yaml` | The per-crew Fargate stack: the execution role (crew-scoped secret read, log write), a task role holding exactly the four `ssmmessages` channel actions, and the log group. Resource names are the ones `identity.py` derives (`kirocrew-crew-<crew>-exec`, `-task`, `/kirocrew/crew/<crew>`) and `taskdef.py` refuses a document that disagrees with, so a rename on either side becomes a launch refusal rather than a mismatch that runs. The task role's whole grant is four `ssmmessages` actions -- the channel the in-task SSM agent opens so a port-forward can reach the crew -- and it must never gain `secretsmanager:GetSecretValue`: the execution role holds that read, scoped to the one crew's secret namespace. Each role carries its OWN permissions boundary, both required parameters with no default, because a boundary caps to the intersection of identity policy and ceiling: `kirocrew-crew-boundary` permits exactly those four actions and `kirocrew-crew-exec-boundary` permits exactly the execution role's seven (the crew secret read, two log-stream writes, four ECR reads). One shared ceiling cannot fit both -- sized for the task role it denies the secret fetch and log-stream open ECS performs before the container starts, so no task launches; sized as their union it would admit the secret read under the role a prompt can reach. |

The two Fargate stacks publish the fields the launch engine's refusal at
`fargate_engine.py` lists, so each has exactly one place to be read from. The
base stack outputs `ClusterName`, `ClusterArn`, `SecurityGroupId`, `SubnetIds`,
`CpuArchitecture`, `Region` and `StackTag`; the crew stack outputs `Crew`,
`ExecutionRoleArn`, `TaskRoleArn`, `LogGroupName`, `SecretNamePrefix` and
`SecretArnPattern`. `SecretArnPattern` is asserted character-for-character
against the policy that grants the read, so an output cannot promise reach the
role does not have. The templates are verified statically and offline by
`test/test_fargate_scaffolding_contract.py`, which calls the `identity.py` and
`taskdef.py` derivation functions rather than restating their strings.

## Provisioning shape

CloudFormation stack, one `aws cloudformation deploy` (change-set based), atomic
rollback, one-command `delete-stack` teardown. AMI resolves from the public
`resolve:ssm` Amazon-Linux-2023 alias per arch (no hardcoded AMI ids). A
`WaitCondition` + `cfn-signal` blocks the deploy until the gateway is healthy; a
failed bootstrap folds the on-box setup-log tail into the signal reason so the cause survives the rollback. Bootstrap failure reasons are normalized to printable ASCII before CloudFormation receives them; otherwise CloudFormation replaces the setup error with a charset error and masks it during rollback (`test_cloud_ec2.py::test_failure_reason_is_filtered_to_printable_ascii`).

Bootstrap is reboot-resilient. UserData performs no package or build work directly:
it first copies its already-rendered script to
`/usr/local/sbin/kirocrew-bootstrap`, installs and enables the
`kirocrew-bootstrap.service` systemd oneshot, starts it without blocking, and
exits. If an account-level State Manager association such as
`AWS-RunPatchBaseline` reboots a newly-online managed node during npm/Vite,
systemd starts the same rendered script again on the next boot. Durable markers
under `/var/lib/kirocrew-bootstrap/` checkpoint installation separately from
successful WaitCondition delivery: a reboot after installation retries gateway
health and the success signal without replacing the checkout, while a reboot
before installation completes stops any partially enabled gateway and safely
reruns the idempotent install. Once `signal-complete` exists, later host reboots
skip bootstrap and only the normal gateway service starts.

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

The optional SSH CIDR is also **normalized** (host bits cleared, `1.2.3.4/24` →
`1.2.3.0/24`) so the SG ingress rule is canonical. `get_stack_failures` sorts the
specific bootstrap reason ahead of CloudFormation's generic `[WaitCondition]`
cascade lines (events are newest-first, so the generic line would otherwise bury
the root cause), and drops the generic noise entirely once a specific reason
exists.

Teardown removes the uploaded source object as part of the contract: after a **confirmed** `delete-stack`, `source.delete_source` returns `{removed, uri, error}`. If the stack delete itself did not confirm, the source object and `last_tag` pointer are preserved and the CLI exits non-zero.

The automatic delete is owner-pinned: `source.delete_source()` issues `s3api delete-object` with `--expected-bucket-owner`, which `test_cloud_source.py::test_delete_source` pins. The pin is load-bearing because a bucket name freed by teardown can be re-registered by another account, and only the `s3api` form accepts it -- `aws s3 rm` has no equivalent flag.

When that delete fails, `cli_cloud._cloud_destroy()` prints an unpinned `aws s3 rm <uri>` as the manual fallback, with no profile, region, or owner pin. That fallback drops the anti-squat guarantee the rest of this module maintains, so an operator who follows it can delete against a replacement bucket instead of the one teardown owned. The owner-pinned equivalent is `aws --profile <profile> --region <region> s3api delete-object --bucket <bucket> --key <key> --expected-bucket-owner <account>`.

## The Fargate lane's configuration home

`cloud.json` gains an optional `fargate` block. It is the only place the Fargate
lane is configured today: there is no wizard step and no dashboard renderer, so an
operator writes it by hand.

| Field | Meaning |
|---|---|
| `cluster` | the ECS cluster the task runs in |
| `subnets` | list of subnet ids; at least one required |
| `security_groups` | list of security-group ids; at least one required |
| `image` | the container image, **digest-pinned only** (`<repo>@sha256:<64 hex>`) |
| `secrets` | list of `[canonical name, ARN]` pairs; one must be named for the model credential |
| `cpu_architecture` | `X86_64` or `ARM64`; defaults to `X86_64` |
| `assign_public_ip` | JSON boolean; defaults to `false` |

**Incomplete means absent.** A block missing any required field, naming a movable
image tag, carrying a secret entry that is not a two-string pair, or carrying an
`assign_public_ip` that is not a JSON boolean, leaves the lane **unregistered**
rather than registered and refusing every launch. The credential secret is part of
that judgement because `taskdef` refuses a definition that delivers none, so a
block without it would register a lane that rejects every launch through it. The
whole secret **set** is judged, by calling the engine's own `secret_destinations`
and `sole_binding`: a valid credential reference sitting beside a malformed one, or
beside one belonging to a different crew, voids the block rather than registering a
lane whose every launch then fails. A present-but-non-boolean `assign_public_ip`
voids the whole block rather than being coerced, because coercion would read the
string `"false"` as true on the one field that decides network exposure. For the
same reason **no** string-typed field is coerced: `str()` would turn JSON `false`
into the non-empty string `"False"` and register a lane against a cluster that does
not exist. The rejection is written once over the dataclass's string fields, so a
field added later is covered without a new branch.

The block is read **per call**, so editing `cloud.json` takes effect on the next
request and deleting the block removes the lane, with no gateway restart. A read
failure yields no lane rather than an exception, because the read happens while the
provisioner list is being built and raising would hide the `aws_ec2` lane too.

## Where launch state lives

Two files, two owners. `cloud.json` is the OPERATOR's: they hand-write the `fargate` block
into it, and the product only reads it. `cloud_launch_state.json` is the PRODUCT's: the
launch path writes the profile, region and tag it decided, and nothing hand-edits it.

| | `cloud.json` | `cloud_launch_state.json` |
|---|---|---|
| written by | the operator | the launch path only |
| read for | the `fargate` block; the legacy pointer | profile, region, `last_tag` |
| product writer | none | `LaunchState.record` / `clear_tag` |
| sealed against agent writes | yes, defence in depth | yes: its tag selects a `destroy` target |
| an aliased name | refused where a launch consumes the block | refused where a command consumes the tag |
| lock | none: no product writer | one, serialising the launch write against `destroy`'s clear; a lock that cannot be taken RAISES rather than writing unlocked, and each writer's caller answers that `OSError` |

They were one file, and every collision followed from that. A post-deploy write into a file a
person may have left mid-edit has to choose between overwriting their bytes and refusing.
Refusing lands after the deploy is billed and before sign-in, so the command aborts over a
pointer; overwriting loses the block. Splitting the owners removes the choice.

**`cloud resume` still re-attaches on an existing install.** Its pointer is in `cloud.json`,
so `LaunchState.load()` falls back to those fields when the record holds none. The fallback is
read-only -- migrating by writing would reintroduce the write being removed, and it would
write on a READ, so any `cloud` subcommand would touch the operator's file. Once a launch has
written the record, the record wins, so a stale pointer in `cloud.json` (which nothing clears
now) cannot outrank the launch that actually happened, and a tag `destroy` cleared stays
cleared.

**A write failure does not fail the command.** `wizard._record_launch` swallows `OSError`,
warns, and prints how to reach the instance. Every caller runs after its remote work, and
sign-in, the tunnel and the closing instructions all matter more to the operator than a
pointer -- which `kirocrew cloud list` can rediscover from the real stacks anyway.

## Security model

- **No stored credentials.** `cloud.json` holds profile name + region + tag, plus
  the `fargate` block's placement and its secret **names and ARNs** -- identifiers,
  never values. The task's execution role fetches each secret's value from Secrets
  Manager before the container starts. A name is carried beside its ARN because an
  ARN alone cannot say where the secret's name ends: the service appends a
  six-character suffix and nothing marks the boundary. The `aws` CLI resolves AWS
  credentials via its own provider chain. Env-var-only
  credentials are unsupported (the sandbox scrubs `AWS_SECRET*`/`AWS_SESSION*`);
  `env_credentials_hint()` detects that and prints an actionable message.
- **The credential recipient is confirmed at launch.** `cloud.json` is dangerous
  only because of what it decides: `fargate.image` chooses the container a launch
  runs, and the task's execution role delivers the model credential into it. So the
  launch path resolves that recipient and requires the operator to have confirmed
  it. `FargateConfig.credential_recipient()` renders the pair that decides it --
  the digest-pinned image and the ARN of the secret carrying the credential -- and
  `POST /api/cloud/launch` carries the operator's confirmation as
  `confirm_recipient`. `FargateLaunchEngine.provision` resolves the recipient from
  the spec it is about to launch, compares it, and refuses an empty or differing
  value before `RegisterTaskDefinition` and before `RunTask`, naming both values.
  A rewritten block therefore produces a **refused launch the operator sees**
  rather than a silent substitution. The confirmation does not come from the file
  and is not persisted, so nothing here depends on a filesystem property; one
  renderer serves both sides, so the shown value and the launched value cannot be
  spelled differently. A launch job resumed after a gateway restart carries no
  confirmation and is refused, because persisting one would put the answer on disk
  beside the file it is meant to be independent of.
- **Nothing in the product writes `cloud.json`.** The launch path's own profile,
  region and tag live in `cloud_launch_state.json` (`launch_state.py`), so there is
  no post-deploy write into the operator's file: nothing to refuse, nothing to
  clobber, and no cross-process lock to hold. `config.py` exposes no writer at all,
  which is asserted on the module rather than on one path -- no function in it calls
  a writing primitive.
- **`cloud.json` is also sealed against agent writes**, as defence in depth rather
  than as the property the design rests on. It is in
  `security.paths._WRITE_PROTECTED_HOME_PATHS`: readable, because the gateway reads
  it on every request to build the provisioner list, but not writable through an
  agent's file-edit tool. Nothing in the product writes it, so no product writer
  needs exempting from that gate. Known limitation, scoped separately: the crew-home
  leaf-seal mechanism this entry uses is per-leaf and shared by every governance
  leaf, so strengthening it is a change to all of them rather than to this one.
- **The launch record is sealed too**, on all three layers `cloud.json` uses: the
  file-write gate, the OS read-only seal, and absent-file pre-creation so the seal
  has a file to bind to. Its reason is its own rather than inherited -- the tag in it
  is what `cloud destroy` resolves when no `--tag` is given, so a writable copy lets
  an agent choose which stack a `destroy --yes` deletes. Moving the tag out of
  `cloud.json` moved that power with it, so sealing the record is what finishes the
  move rather than one more door. It is NOT in the strict no-alias list: that list is
  walked where a spawn is prepared, so a leaf in it refuses every sandboxed spawn on a
  host whose files legitimately carry a second name. The record's own alias refusal
  sits at its consume seam instead, in the bullet below.
- **A write to the record can only ever leave "no target", never "the wrong target".** The two
  halves are opposites deliberately. BEFORE provisioning a new stack, the pointer to the
  PREVIOUS one is cleared and a failure ABORTS the launch (`wizard._clear_prior_pointer`) --
  nothing is created and nothing bills, so an abort is free. AFTER the deploy confirms, a
  failed write only WARNS (`wizard._record_launch`, and `destroy`'s own `clear_tag`) -- the
  irreversible work is done and cannot be retried, so losing the pointer costs a
  `kirocrew cloud list`. Without the first half the second is destructive rather than lossy: a
  failed post-deploy write would leave the record naming the old stack while the new one is
  what exists, and a later `destroy` with no `--tag` would delete the old one. A DECLINED clear
  aborts the launch too when a different tag is saved: declining means another launch recorded
  its own stack, so provisioning would make that stack's tag the saved one. The decision uses
  the tag the compare saw (`LaunchState.try_clear_tag`, inside the same lock hold), not the
  bool, because a decline also happens when no pointer is saved at all -- which is the state
  the clear exists to reach and is safe to launch from.
- **An aliased launch record is refused where a command CONSUMES its tag**, in
  `LaunchState.load` (`sandbox.require_unaliased_launch_state`). All three seal layers
  name a PATH, and a symlink at the name or a second hardlink to the inode is reachable
  by a name none of them covers -- so a write through it puts any tag in the record and
  `cloud destroy --yes` deletes the stack it names, with `cloud launch` re-attaching to
  it as the same substitution one step quieter. **The legacy fallback is guarded too**: when
  the record holds nothing, `load` reads `cloud.json`'s fields, and that file is then the
  source of the tag, so `require_unaliased_cloud_config` runs before it is read. Its own
  launch-seam guard (`defaults.engine_for`) never runs on a teardown, and the empty record
  the sandbox pre-creates is the default state, so without that call the same hole stayed
  open through the old file. The refusal takes the path being read
  rather than deriving it, so the file checked and the file consumed cannot differ. It
  is a named refusal, not an escaping exception: `handle_cloud` catches it for the whole
  dispatch table and prints the file, the shape and one command that clears it. That command
  is rendered for the operator's own platform by `sandbox._delete_file_command` -- `rm` with
  POSIX quoting, or `del "<path>"` on Windows, which is a `cmd` builtin and a PowerShell alias
  where `rm` is neither and POSIX single quotes are not quoting characters. The platform is a
  PARAMETER, not only a read of `IS_WINDOWS`, so both renderings are testable from one host.
  `clear_tag` deliberately uses the unguarded read -- it runs after `destroy` has already
  deleted the stack, and a refusal there would abort a command whose irreversible work is done.
- **An aliased governance leaf WARNS on the spawn path, on every platform.**
  `_materialize_sealable_ceilings` runs from `namespace_argv` on every Linux
  sandboxed spawn, so a refusal there refuses all of the host's agent work -- chat,
  cron, subagents -- whenever a leaf carries a second name, which is what stow,
  chezmoi and `rsync --link-dest` leave behind. The strict refusal for `cloud.json`
  lives where a launch consumes the file instead
  (`require_unaliased_cloud_config`, called from the provisioner seam), so the alias
  costs one lane's launch rather than the box. `playwright-cli` keeps main's own
  strict DIRECTORY rule, which this change does not touch.
- **An aliased `cloud.json` is refused where a launch CONSUMES it**, in
  `DefaultRemoteProvisionerProvider.engine_for`, and only warned about on the
  universal spawn path (`sandbox._warn_aliased_strict_leaves`). Refusing on the spawn
  path failed every sandboxed spawn on the host -- chat turns, cron jobs, subagents --
  when the file carried a second name, which is what stow, chezmoi and
  `rsync --link-dest` leave behind. The exposure is one lane's launch, so that is what
  the refusal costs.
- **Injection closed in depth.** tag/region/profile/CIDR/repo/ref/run_as are
  charset-validated (`validation.FieldSpec`) before reaching argv; `ec2.validate_profile()` aliases `deploy.profiles.PROFILE_SPEC`, which admits `+` in IAM Identity Center-derived profile names while excluding option-shaped names, so valid profile names remain usable without weakening argv validation; **and** the
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
- **Create verbs require the managed request-tag (per-resource-ARN split).**
  `ec2:RunInstances` and `ec2:CreateSecurityGroup` require
  `aws:RequestTag/kirocrew:managed=true`, so a leaked launcher credential can't
  create untagged instances/SGs that sit outside the tag-gated
  Stop/Terminate/Delete/Authorize statements. Because these calls authorize
  **per-resource** across ARNs that don't carry the tag (RunInstances also creates
  an untagged volume + ENI and references an existing image/subnet/SG; this
  template's `TagSpecifications` tag only the instance), a blanket request-tag
  403s the launch — so the condition is split by ARN: `RunInstances` requires the
  tag on `instance/*` only (`Ec2RunInstancesTaggedInstance`) with the
  volume/ENI/referenced ARNs granted unconditioned
  (`Ec2RunInstancesSupportingResources`), and `CreateSecurityGroup` requires it on
  the new `security-group/*` (`Ec2CreateSecurityGroupTagged`) with the referenced
  `vpc/*` unconditioned. Proven with a least-privilege assumed-role `run-instances
  --dry-run` (tagged instance ALLOWED incl. the template-shaped call, untagged
  DENIED; tagged SG ALLOWED, untagged DENIED).
- **Escalation primitives constrained + immutable, pre-created permissions
  boundary.** `ec2:CreateTags` is gated by an `ec2:CreateAction` condition
  (`RunInstances`/`CreateSecurityGroup`) so it can only tag at creation — a holder
  can't tag an *existing* resource `kirocrew:managed=true` to pull it under the
  tag-gated Stop/Terminate/Delete statements. `iam:AttachRolePolicy`/`DetachRolePolicy`
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

## Fargate task definitions as data

A second compute backend for remote instances runs a crew as a Fargate task
rather than an EC2 host
([rfc-remote-instance-on-fargate](../../request-for-change/rfc-remote-instance-on-fargate.md)).
`fargate/` produces the two AWS payloads that backend sends, as values. Nothing
in it calls AWS: `run_aws` refuses a non-read-only call from an agent session and
carries no `ecs` pair in its allowlist, so a launch engine performs the calls and
this package decides what they would say. That split is what makes the two
security decisions a crew task carries reviewable without a credential, because
each is a property of a `dict` that a test can state.

### What a revision is keyed on

One task-definition family per crew, one revision per (image digest, secret ARN
set, cpu architecture, log configuration), registered on demand against a cached
ARN. The key is dictated by the API, not chosen: `RunTask` can override `cpu`,
`memory`, `ephemeralStorage`, `taskRoleArn`, `executionRoleArn` and a container's
`command` and `environment`, and it cannot override `image`, `secrets`,
`logConfiguration` or `runtimePlatform`. Those four are therefore the only fields
a launch cannot bend at run time, so they are the only ones that can force a new
revision. Steady state is one API call, two on the first launch of a new digest.

Two consequences follow. Keying on size is wrong because size is an override, and
`TaskDefinitionSpec` carries no size field, so it is absent as an input rather
than excluded from a hash. The same now holds for both roles, which `RunTask` can
also override: nothing `RunTask` can override is a field of the spec at all, so
the key cannot read one. Registering per launch is wrong, because
`RegisterTaskDefinition` has no upsert and leaks a revision on every call.

`portMappings`, `networkMode` and `requiresCompatibilities` are equally beyond
`RunTask`'s reach and are still not in the key: the key is the fields that are
unoverridable **and** variable, and the front port is a constant of the image.
`FRONT_PORT` is a module constant for that reason, and a `RunTask` environment
override naming `SMC_FRONT_PORT` is refused, since a task whose front port
disagrees with the declared `portMappings` is unreachable and an unreachable task
is indistinguishable from a slow one.

A registered revision carries its own key as the `kirocrew:revision-key` tag.
That is not telemetry: without it a lost local cache has no witness, the launcher
re-registers, and the revision leak the key exists to prevent happens anyway.
The account can answer what a revision was keyed on.

Both emitted payloads carry `kirocrew:managed=true`, and its value is derived. The
rest of the code base matches this tag **by value**, not merely by key: `ec2.py`
discovers with `Key=kirocrew:managed,Values=true` and refuses a stack whose value
is not `true`, and `iam.py` conditions resource permissions on
`aws:ResourceTag/kirocrew:managed` equalling `true`. A task carrying any other
value is therefore a running task teardown does not enumerate and the teardown
role is not permitted to stop, billing in the owner's account with nothing
pointing at it. The caller's own correlation value has its own key,
`kirocrew:launch`, because the two answers have opposite requirements: the marker
must be constant for teardown to match it, and the correlation value must vary for
a caller to find one launch. `ec2.py` already separates them the same way, tagging
`kirocrew:managed=true` beside `kirocrew:instance=<tag>`; collapsing both into one
key let a caller's value displace the marker.

### The credential reaches the container through the definition

The model credential arrives as `secrets[].valueFrom`, fetched by the **execution
role** before the container starts. `ContainerOverride` has no `secrets` field,
so the only way to put a credential in a `RunTask` request is `environment`, in
plain text, where it is written to the CloudTrail record of the request and can
be read back out of `DescribeTasks`. The value is a long-lived model credential.

Nothing downstream can tell a wrong credential from a right one. The container's
`require_api_key` proves a key was supplied, not that it is this crew's key, so a
definition naming another crew's secret produces a task that starts, answers, and
serves turns under the wrong identity, silently at both ends.

**IAM is the primary control.** Each crew's execution role is derived per crew
(`kirocrew-crew-<crew>-exec`), so it can be granted that crew's secret and no
other, and a definition naming another crew's ARN fails when the role cannot read
it, before the container starts. **Refusing at generation is defence in depth**,
and it earns its place: a refusal names the problem, while the same mistake
reaching AWS surfaces as a permission error at task start that says nothing about
which crew was confused. It is neither the only barrier between crews nor
redundant with the one that is.

The refusal would be the whole of the guarantee for a deployment that shared one
execution role across crews, because such a role must hold every crew's secret
ARN and its fetch for crew A is then indistinguishable from its fetch for crew B.
This design does not share a role, and the cost of that choice belongs to the
deploy track: **one execution role per crew, not one for the fleet.**

The refusals, each stated as a property rather than as the case that prompted it:

- Every `secrets[].valueFrom` is a full, unambiguous, crew-bearing secret ARN.
  The six-character Secrets Manager suffix is required, because a partial ARN is
  resolved by search and a secret whose name ends in a hyphen and six characters
  resolves that search to a **different** secret. A version or JSON-key tail is
  refused too: a crew credential is the whole secret string.
- The variable a secret lands in is derived from the secret's own name, never
  supplied beside it. `TaskDefinitionSpec.secrets` is a sequence of ARNs, so
  nothing names the destination a second time and a container cannot receive one
  secret's value under another secret's name. Two ARNs deriving the same variable
  are refused, since one variable takes one value and a tie has no winner.
- `environment` is a **closed channel**, not a filtered one. Four review rounds on
  this module found one defect four times, once per variable: a caller could
  contradict the spec through an override, and each fix closed a single name.
  `task_definition`, then the model credential, then `SMC_CONTROL_SECRET`, then
  `SMC_CREW_NAME`. A guarantee that holds only for the names someone has already
  thought of is not a guarantee, so the channel is closed by set membership and
  this module writes the derived values itself.
- A name belongs to the closed set when a caller-supplied value could CONTRADICT
  what the request already asserts, on one of two limbs: it decides **what the
  task is**, which the spec's secrets fix through the crew they name, or it
  decides **who may reach it**, which is the credential set and the trust-domain
  declaration. `SMC_CREW_NAME` and `SMC_SINGLE_PRINCIPAL` are derived and written
  here; `SMC_CONTROL_SECRET`, `KIRO_API_KEY`, `SMC_BUNDLE_DIR` and
  `SMC_FRONT_PORT` are refused and never written. Everything else stays the
  caller's: a bucket cannot contradict the spec, because the spec says nothing
  about buckets.
- Writing `SMC_CREW_NAME` is what gives the container's own
  `manifest crew_name == SMC_CREW_NAME` refusal something to catch. When both
  values came from the caller they could agree with each other while contradicting
  the spec; now pairing one crew's secrets with another crew's image fails inside
  the container with a message naming both crews.
- The container's config module owns which names it reads. A test derives that set
  from `load()` and fails when a name is neither derived, refused, nor
  deliberately left to the caller, so the next variable added there is decided
  before it can arrive as an open channel. The caller-owned entries carry a
  written reason each, because an unexplained allowlist is the next thing to go
  stale.
- A placement with no security group. ECS substitutes the VPC's default group,
  which admits traffic from anything else in it, and the task's front process
  answers a turn and a liveness check without the control secret, so a workload
  sharing that group could take a turn on the crew using the crew's own model
  credential.

### A name is never recovered from a string that can contain its own delimiter

Three places in this module read an identifier out of a string it was handed, and
the same defect was found in two of them a review apart. The practice, stated
generally: **recovering a name by stripping a suffix is unsafe wherever the name
can itself contain the suffix shape.** All three are now on the safe side of it,
by two different mechanisms, because the strings differ in whether they are
derivable.

`parse_role_arn` strips `-exec` or `-task`. A role ARN has no service-generated
component, so re-deriving it from a candidate crew reproduces the input exactly
and the round trip IS the parse: a candidate is accepted only when it rebuilds the
input byte for byte. That is what makes a crew called `a-exec` unambiguous, rather
than a claim about how the pattern backtracks.

`parse_secret_arn` cannot be verified the same way, because Secrets Manager's
six-character suffix is chosen by the service and nothing here can reproduce it. A
secret named `.../KIRO_API_KEY-AbCdEf` has the complete ARN
`.../KIRO_API_KEY-AbCdEf-XyZ123`, and the string `.../KIRO_API_KEY-AbCdEf` is both
that secret's partial ARN and a well-formed complete ARN for a different secret.
So the reader takes a `SecretRef` carrying the canonical name, verifies the ARN is
that name plus exactly one suffix, and reads the destination from the verified
name. A test asserts both readers' signatures require a reference, so a
plain-string entry point cannot arrive as a convenience.

Reading the CREW out of a secret ARN was never in the unsafe class and still is
not. A crew sits between two `/` characters and `/` is outside the crew charset,
so the segment's end is marked in the string rather than inferred. That is why the
document walk still establishes a binding from an ARN alone. It also refuses a
remainder that is not shaped like a name plus one suffix, which keeps the
partial-ARN refusal; checking that shape is a property of the string, while
deciding which part is the variable needs the split point that only a reference
states.

**What this does not do.** Nothing here can confirm that AWS resolves a secret
ARN to the named secret, because that depends on which secrets exist and only
`DescribeSecret` can answer it. This module checks that a reference is internally
consistent. Authority for the pairing belongs to the API that created the secret,
so a caller passes the ARN as `CreateSecret` returned it together with the name it
was given, and never a pair assembled by hand.

### Absent is not a value

Every field of every type in this module refuses empty, zero and unparseable
rather than letting it carry a meaning. The rule is written into the module
docstring because the opposite kept happening one field at a time, and each fix
was correct and too narrow: a missing task size let the registration floor become
the runtime shape, an open `environment` let a caller contradict the spec, an
unparseable ARN was readable to one parser and refused by the other, and an empty
`security_groups` made ECS substitute the VPC default group. Every one of those
produced a request that looked correct.

The sweep is enforced rather than performed once. A test reads
`dataclasses.fields` for each public type and fails when a field is not decided
by an emptiness test, so a field added later cannot arrive as an untested
permissive default. `CrewBinding` validates its partition, account and crew on
construction, so every derived name is well-formed because the binding exists
rather than because a parser was used. `LogSpec` refuses the values that leave a
task running with no readable log stream, and one `validated_region` governs both
an ARN's region and a log configuration's.

The package's EXPORT LIST is held to the same standard, because a name a consumer
cannot import is a name it re-spells. That is not cosmetic: a launch engine that
wrote its own `"kirocrew:task"` rather than importing `LAUNCH_TAG_KEY` classified
every managed task as foreign, so its teardown planner returned an empty delete
set and reported success while every task kept running and kept billing in the
owner's account. Absent from an export list read as a value, exactly as an empty
collection and a missing field had. A constant is therefore on the surface when a
caller cannot build an acceptable input without reading it, or cannot interpret a
produced payload without reading it, and a test derives both limbs from the source
rather than listing names: the refusal limb from the functions that raise, resolved
to a fixpoint through the helper they delegate to, and the payload limb from the
values that actually appear in a produced document and request. The derivation
prefixes and suffixes are deliberately excluded, each with its reason recorded next
to it, because the derived name is already a function and exporting the fragment
would offer a second way to spell what the function returns.

Fargate's valid CPU and memory pairs and its ephemeral-storage bounds are encoded
here rather than left to `RunTask`. Deferring to AWS turns a generation-time
refusal into a launch-time one, which is the failure this module converts.

Two defaults are deliberate exceptions, recorded so they are not later mistaken
for oversights:

- `assign_public_ip = False`, because false is the safe direction and the flag is
  not the boundary. A Fargate task in a public subnet with no NAT gateway cannot
  pull its image without an address, so what bounds who reaches the container is
  the security group.
- A task size equal to the registration floor, because requiring the field
  already removed the silence. The floor arriving because nobody chose was the
  defect; the floor's value was never wrong.

`Placement.cluster` is caller-owned but refused when empty, and the two questions
have different answers. An empty cluster means the account's implicit `default`
cluster, which is absent read as a value. It is not in the closed set, because
nothing in a task-definition spec says anything about a cluster and so no cluster
can contradict one.

- A container `command` override is not offered. Secrets are injected and the task
  role attached before any command runs, so replacing the image's command would run
  arbitrary code holding the model credential with none of the supervisor's sandbox
  verification or environment scrubbing. Pinning the override to the supervisor's
  own command instead would copy the image's entrypoint here and create two places
  to keep in agreement.
- Every crew-bearing ARN anywhere in the produced document resolves to one
  `CrewBinding`: partition, account and crew together, so another account's
  secret is refused by the same code path as another crew's. The check walks the
  finished document rather than reading named fields, so a field added to the
  shape later is covered without anyone extending a list.
- Neither role is an input. `TaskDefinitionSpec` carries no role ARN and no crew
  name: the crew comes from the secrets and both roles are derived from it, so a
  role naming the wrong crew, and a swap of the two, are unconstructible rather
  than refused. The swap matters and agreement on the crew would not have caught
  it, because both of a crew's roles name that crew. A container's model
  subprocess can read the task role's credential out of its own environment and
  act as it, so a task carrying the **execution** role can re-read secrets, and a
  shared execution role holds every crew's.
- The definition that runs is not an input either. `run_task_request` derives the
  family from the spec and takes only a revision NUMBER, so a caller cannot pair
  one crew's spec with another crew's family. That pairing would pass every
  refusal in the module, because each reads the spec while the identifier decides
  what executes. What the module does not establish, and says so in that
  function's docstring, is that revision N holds the content the spec describes:
  that needs the registered definition's `kirocrew:revision-key` tag, which is an
  AWS call. The caller verifies it, and a mismatch is a stale cache.
- The definition delivers the model credential, and a `RunTask` environment
  override may not name anything the definition delivers. Those two are one
  property: the credential always arrives, and it arrives only this way.
- The image is digest-pinned. A movable tag would let the content behind a
  revision key change after the revision was registered, and every property built
  on the key assumes it cannot.

`image` stays an input and the log GROUP no longer is. Which way each fails is the
reason, and the earlier version of this paragraph got the log group wrong, so the
correction is recorded rather than quietly replaced. It argued a foreign group was
safe as an input because a per-crew execution role carries `logs` permission for
its own group only, making IAM the guard. No such role document exists in this
repository: the only `logs`-scoped policy here belongs to the deploy-app track and
is scoped to `/kirocrew-deploy-app/*`. The guard was an assumption about a document
the deploy track has not written, and until it is written a caller-supplied group
decides where the transcript of every turn is stored with nothing checking it.
Nothing else catches it either, because a log group name is not an ARN and so the
document walk that refuses a foreign crew's ARN never sees one. The group is
therefore derived from the crew where the document is built, and `LogSpec` carries
only the region and the stream prefix, which cannot name another crew: the region
selects a regional endpoint, and the prefix distinguishes streams inside a group
the crew already fixes. One image serves every crew by design, so its registry
account is not a crew-identity question, and the content behind it is pinned by
digest.

The task role holds no `secretsmanager` permission. That belongs to the role
documents rather than to these payloads, and the structural half is here: the two
roles are never the same ARN, and only `executionRoleArn` sits where a secret is
fetched.

`RunTask` accepts `executionRoleArn` and `taskRoleArn` as overrides, and neither
is ever emitted. Overriding the execution role would reopen exactly the hole the
document's agreement check closes, running a definition that names one crew under
another crew's fetcher. The override key sets are closed and are enforced against
the produced request, so widening them takes an edit to the allowlist in the same
change.

### What refusal does not cover

A `valueFrom` that agrees on the crew but names a secret that does not exist is
accepted, and that is deliberate rather than an omission. Existence is a property
of the account at task-start time, not of the document, so no pure function
decides it and a check would be a read that can go stale before the launch. More
to the point, the two failures are not the same shape. A nonexistent secret fails
the execution-role fetch before the container starts, so the task never runs,
`require_api_key` never executes, no turn is served, and the operator sees
`ResourceInitializationError`. A crew disagreement succeeds. Only the silent
failure has to be unrepresentable; the loud one can be left to fail loudly.

The same reasoning covers an invalid Fargate cpu/memory pair, which `RunTask`
rejects outright, and is why the definition's registration floor
(`REGISTRATION_CPU`/`REGISTRATION_MEMORY`) is never allowed to become an
effective size: `run_task_request` requires a `TaskSize` and refuses one that is
not a positive integer of Fargate units. A floor that silently became the running
shape would be the quiet failure this module exists to avoid.

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

`test/test_fargate_{identity,taskdef,runtask}.py` need no mock at all, because
the modules under test perform no I/O. Their assertions are written against the
property rather than an instance of it: the spec-field table that decides which
fields the revision key reads is checked against `dataclasses.fields`, so a field
added later fails the suite until it is classified; the cross-crew refusal is
parametrised over every ARN-bearing position and every part of the identity; and
the no-role-in-the-request check walks the whole produced request instead of
reading the two keys a defect was first found under.
