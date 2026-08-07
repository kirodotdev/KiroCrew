# Cloud Launcher Module

## Overview

`src/kiro_crew/cloud/` runs KiroCrew on the user's **own** AWS EC2 instance with a
single command. It provisions a CloudFormation stack, ships the local source,
signs `kiro-cli` in over SSM, and opens the dashboard through an SSM
port-forward. Command surface (wired in `cli_cloud.py`, invoked via
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
| `ec2.py` | `deploy`/`status`/`stop`/`start`/`destroy` via `aws cloudformation` + `ec2`; AZ- **and egress-**aware `discover_network`; tag-based stateless discovery; `_validate_cidr`. `find_stack` verifies BOTH `kirocrew:managed=true` AND `kirocrew:instance==<tag>` before status/stop/start/destroy touch a stack — so a same-prefix managed stack with a different instance tag can't be acted on by the wrong `--tag`. |
| `iam.py` | Least-privilege launcher policy generator (applied by the user, never by KiroCrew) + read-only reachability check + the **content-fixed instance permissions-boundary document** (`boundary_policy_document`/`boundary_arn`) and its constants (`BOUNDARY_NAME`). |
| `ssm.py` | SSM `send-command` run-and-poll (base64-wrapped remote scripts) + `start-session` port-forward; `port_is_free` / `wait_for_local_port`. |
| `login.py` | `kiro-cli` device-code / social sign-in on the box over SSM. |
| `connect.py` | SSM port-forward + token mint + open browser; Instances-registry integration; `redact_token`. |
| `source.py` | Package the local checkout (`git archive`, tarfile fallback) and upload to a per-account S3 bucket; secret-excluding filter shared by both paths. Also **`ensure_instance_boundary`** — creates the shared, immutable `kirocrew-ec2-boundary` managed policy once (create-if-not-exists, never re-versioned) and returns its ARN; `delete_instance_boundary` for admin cleanup. |
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

Because the public repo is private, the box can't `git clone` it: the launcher
packages the local checkout and uploads it to a launcher-owned bucket
(`kirocrew-src-<account>-<region>`); the instance downloads it with its own IAM
role (`s3:GetObject` scoped to the single object). A public `git clone` remains
a fallback when no `SourceBucket` is passed.

`discover_network` is **egress-kind-aware**, not just "has a default route":
`_subnet_egress_kinds` classifies each subnet's effective route table (explicit
association, else the VPC main table) as NAT (`NatGatewayId`/`NetworkInterfaceId`
default route) or IGW (`igw-` default route). It prefers a **NAT** subnet (works
regardless of a public IP), then an **IGW** subnet — and the template's Instance
`NetworkInterfaces` block forces `AssociatePublicIpAddress: true`, so an
IGW-routed subnet works even when its `MapPublicIpOnLaunch` is false. A subnet
with only a local route (no 0.0.0.0/0 egress) is never chosen — the deploy would
otherwise hang to the `WaitCondition` timeout. An **explicit** route-table
association overrides the main table even when it has no egress: a subnet bound
to a local-only table is treated as no-egress (excluded from the main-table
fallback), so it can't be mistaken for having the main table's egress. The
optional SSH CIDR is also **normalized** (host bits cleared, `1.2.3.4/24` →
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

## Bootstrappers

`install.ps1` (Windows client) and `cloud-install.sh` (macOS/Linux) ensure the
`aws` CLI + `session-manager-plugin` + Python are present, then hand off to
`kirocrew cloud launch`. They install *client* prerequisites only — the gateway
always runs on the Linux EC2 box, never on Windows.

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
