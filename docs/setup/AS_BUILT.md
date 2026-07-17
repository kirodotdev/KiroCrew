# KiroCrew Cloud Launcher — As Built

What actually shipped, and the things live end-to-end validation on a real AWS
account taught us (that the plan didn't anticipate).

## Command surface

```
kirocrew cloud launch     # provision + configure + open dashboard (interactive)
kirocrew cloud list       # your live cloud instances
kirocrew cloud status     # one instance's stack + EC2 state
kirocrew cloud connect    # (re)open the dashboard over an SSM tunnel
kirocrew cloud stop|start # pause / resume (save cost)
kirocrew cloud destroy    # remove EVERYTHING from AWS (clean uninstall)
kirocrew cloud iam-policy # print the least-privilege IAM policy to apply
kirocrew cloud iam-boundary # (admin, one-time) pre-create the immutable instance boundary
kirocrew cloud doctor     # check prerequisites + AWS reachability
```

Bootstrappers for a machine with nothing installed: `install.ps1` (Windows
client) and `cloud-install.sh` (macOS/Linux) — they ensure `aws` CLI +
`session-manager-plugin` + Python, then hand off to `kirocrew cloud launch`.

## Module map (`src/kiro_crew/cloud/`)

| Module | Role |
|--------|------|
| `aws.py` | The single `run_aws` chokepoint (sandbox-wrapped, `--profile`, never boto3) + `AccessDenied → action` mapping. |
| `sizes.py` | arm64/Graviton size tiers; 16 GB default (`t4g.xlarge`). |
| `iam.py` | Least-privilege policy generator (applied by the user) + read-only reachability check. |
| `source.py` | Package the local source (`git archive`), upload to a per-account S3 bucket. |
| `ec2.py` | `deploy`/`status`/`stop`/`start`/`destroy` via `aws cloudformation` + `ec2`; AZ-aware network discovery; tag-based statelessness. |
| `ssm.py` | SSM `send-command` run-and-poll + `start-session` port-forward. |
| `login.py` | `kiro-cli` device-code sign-in on the box over SSM. |
| `connect.py` | SSM port-forward + token mint + open browser; Instances-registry integration. |
| `config.py` | Persisted profile/region/tag (**never credentials**). |
| `ui.py` / `wizard.py` | Terminal UI + the 6-step interactive launch flow. |
| `templates/kirocrew-ec2.yaml` | The CloudFormation stack. |

CLI wiring is `cli_cloud.py` (thin dispatchers); `cli_setup.py` gains a
delegating `_maybe_setup_cloud()` step. No AWS logic lives in the CLI layer.

## Key architecture decision the plan missed: shipping the source

The public repo is **private**, so the EC2 box cannot `git clone` it. The
launcher therefore **packages the local checkout and uploads it to a
launcher-owned S3 bucket** (`kirocrew-src-<account>-<region>`); the instance
downloads it with **its own IAM role** (`s3:GetObject` scoped to that one
object). Result: private-repo safe, still **zero credentials on the box**, and
robust to any repo size. A public `git clone` remains as the fallback when no
`SourceBucket` is passed.

## Provisioning shape (unchanged from the plan)

CloudFormation stack, one `aws cloudformation deploy`, atomic rollback,
one-command `delete-stack` teardown. Highlights:

- **`resolve:ssm` AMI alias** — always the latest Amazon Linux 2023 for the arch;
  no hardcoded AMI IDs (guarded by a test).
- **SSM Session Manager** — no inbound ports, no SSH key. Connect is an
  `AWS-StartPortForwardingSession`. SSH is an opt-in fallback (`AllowSshCidr`).
- **`WaitCondition` + `cfn-signal`** — `deploy` blocks until the gateway is
  actually serving; a failed bootstrap rolls the stack back cleanly.
- **AZ-aware, egress-verified subnet pick** — chooses a subnet in an AZ that
  actually offers the instance type **and** has a verified internet-egress route
  (internet gateway or NAT in its effective route table). A public-IP flag alone
  doesn't prove reachability, so a private-only VPC fails fast with guidance
  instead of hanging until the WaitCondition times out.

## Fixes that only live testing on a real account surfaced

Each of these caused a real launch to fail and roll back; the fix is in the
shipped template/code, and most have a regression test.

1. **Non-ASCII in a security-group `GroupDescription`** — EC2 rejects it. All
   template *property values* are now ASCII (em-dashes removed); a test asserts
   it.
2. **AZ mismatch** — the default VPC's first subnet was in `us-east-1e`, which
   doesn't offer `t4g.xlarge`. `discover_network` now queries
   `describe-instance-type-offerings` and picks a supported-AZ subnet.
3. **Node package name** — `nodejs20` doesn't exist on AL2023; install the distro
   `nodejs npm` (v18, enough for the vite build) with NodeSource as fallback.
4. **kiro-cli glibc** — AL2023 ships glibc 2.34 but the standard kiro-cli build
   needs 2.39+; use the **musl** build (validated: `kiro-cli 2.11.1` runs).
5. **Python version** — `install.sh` needs Python ≥ 3.10 but AL2023's default
   `python3` is 3.9; install `python3.11` in user-data so `install.sh` finds it.
6. **`tee` + `pipefail` in user-data** — `install.sh`'s progress spinner streams
   many `\r` writes through a pipe; a `tee`-via-process-substitution plus
   `pipefail` turned a harmless SIGPIPE into a fatal mid-install SIGTERM. Log via
   a plain file redirect; don't set `pipefail`.
7. **`sudo` env stripping** — passing `VAR=value sudo -u user bash -lc '...'` was
   silently dropped by sudo's `env_reset`, emptying `SourceBucket` and taking the
   git-clone branch. The fetch step now writes a script with the values inlined
   (via `!Sub`) and runs it — no env passed through `sudo`.
8. **`list` showed terminated instances** — the tagging API returns terminated
   instances for a while; `list_instances` now filters them out.
9. **NodeSource GPG key URL rotated → bootstrap 404** — NodeSource retired the
   old `rpm.nodesource.com/gpgkey/nodesource-repo.gpg.key` (now HTTP 404), so when
   the primary `dnf install nodejs npm` hit a transient AL2023 cache race (a
   missing `libbrotli-*.rpm` mid-transaction) the NodeSource fallback then died
   with "could not import NodeSource GPG key" and every launch rolled back at the
   WaitCondition. Fixed in the template: a `dnf clean packages && dnf makecache`
   retry-once before the fallback, and the fallback key URL switched to the
   current `rpm.nodesource.com/gpgkey/ns-operations-public.key` (verified live:
   200 + valid PGP block; `pub_20.x/nodistro` repomd 200 on both arches). After
   this fix the bootstrap gets **past** Node.js install (Node v18.20.8, npm
   "added 902 packages"), confirming the fix works. (A `[3/5] Build → tsc -b &&
   vite build` failure that then appeared — "transforming..." then "Terminated" —
   was first mis-hypothesized as a memory/OOM limit, but forensics disproved that:
   see item 10. It was the AL2023 first-boot reboot, NOT memory and NOT the build.)
10. **AL2023 first-boot SELinux reboot races user-data → SIGTERM mid-build.**
   The real cause of the `[3/5] Build … Terminated` failures. Amazon Linux 2023
   ships `/etc/cloud/cloud.cfg.d/40_selinux-reboot.cfg`, a cloud-init
   `power_state` drop-in that reboots the box ONCE on first boot (`test -f
   /run/cloud-init-selinux-reboot`, "Rebooting machine to apply SELinux kernel
   commandline setting"). That reboot fires ~7 min into first boot — straight
   through the long `dnf` + `npm ci` + `vite build` — and its `reboot.target`
   "Sending SIGTERM to remaining processes" kills the in-flight build (seen as
   "Terminated" mid-`transforming...`), then the WaitCondition fails. Forensics
   that nailed it (on a `--keep-on-failure` box): `uptime -s` was ~7 min AFTER the
   stack launch (the box rebooted), the journal showed `systemd-logind: The system
   will reboot at …` → `Reached target reboot.target` → SIGTERM, and there was NO
   OOM anywhere in `dmesg`/journal (swap present + unused, ~7.4 GB available). This
   explains why a manual `npm run build` and a full warm `install.sh` re-run both
   succeed (the reboot flag is consumed on the first boot, so a warm box never
   reboots) and why the kcfetch retry-once never fired (a reboot is not a nonzero
   exit — it kills everything). Fixed in the template UserData, early (right after
   `fail()` is defined, before the heavy work): `rm -f
   /run/cloud-init-selinux-reboot` (remove the trigger flag) + `rm -f
   /etc/cloud/cloud.cfg.d/40_selinux-reboot.cfg` (neuter the drop-in) + `shutdown
   -c` (cancel any already-scheduled reboot). This works by cloud-init ordering —
   `scripts-user` (our UserData) runs strictly before `power-state-change`, so the
   flag is gone before the condition is evaluated. The box runs SELinux Permissive,
   so skipping the cosmetic kernel-cmdline tweak is safe. A 4 GB swapfile is also
   provisioned as cheap defense-in-depth headroom (NOT the primary fix — the build
   peaks ~3.4 GB, measured). NB on validation semantics: `PingStatus=Online` proves
   OS boot + SSM-agent registration (enough for the IAM/boundary/SSM-core checks),
   but the strict bar for "launch works" is the stack reaching `CREATE_COMPLETE`
   AND `kirocrew cloud connect` → dashboard HTTP 200.

## Validated end-to-end on the dev account

`launch` → `CREATE_COMPLETE` (box booted, pulled source from S3, installed
KiroCrew + kiro-cli, `kirocrew.service` active, gateway HTTP 200) →
`kirocrew cloud login` drove the kiro-cli device-code flow (Builder ID),
verified signed-in after browser approval → `connect`/`tunnel` opened an SSM
tunnel and the dashboard answered HTTP 200 on `localhost` with a minted token
→ `destroy` deleted the stack, IAM role, instance, security group, volume, and
the S3 source object. No orphaned resources.

### Note on "Backend hiccup — retrying…" in chat

If a chat shows "Backend hiccup" after sign-in, that is a **Kiro account model
entitlement**, not a launcher bug. kiro-cli's default `auto` model can route to
a model (e.g. `claude-opus-4.8`) the account isn't entitled to on Bedrock,
which surfaces as "Model ... is unavailable". Pick an available model in the
dashboard's model picker (a free Builder ID account offers e.g.
`claude-sonnet-4.5`, `claude-haiku-4.5`, `deepseek-3.2`) and chat works. Run
`kiro-cli chat --list-models` on the box to see what the account can use.

## Security posture (preserved)

- Provisioning is a **human/installer action, never an LLM tool** — the cloud
  verbs are not MCP tools, and both the destructive AWS CLI verbs
  (`aws ec2 terminate-instances`, `aws ec2 delete-*`,
  `aws cloudformation delete-stack`) **and** the `kirocrew cloud
  destroy|stop|start|launch|connect|tunnel|login` wrappers are blocked for the
  agent by the `deniedCommands` regexes in `config/defaults.json` (enforced by
  kiro-cli on `execute_bash`/`shell`) — only read-only `list`/`status` stay
  agent-accessible. Note this is a *different* mechanism from `security.py`'s
  `BUILTIN_DENY_PATTERNS`, which use underscored MCP-tool-name shapes
  (`*terminate_instance*`) and don't match the hyphenated CLI strings.
- KiroCrew stores **no** AWS credentials (only a profile name + region + tag).
- The gateway binds **loopback only** on the box; access is always tunnel + token.
- **IMDSv2 enforced** on the instance (`HttpTokens: required`, hop limit 1): the
  box runs a prompt-injectable agent, so an SSRF/injection reaching the metadata
  endpoint can't read the instance role's STS credentials via IMDSv1.
- **Launcher IAM is tag-scoped for the dangerous verbs** — `ssm:StartSession` /
  `ssm:SendCommand` (RCE-adjacent) and the EC2 destructive verbs
  (`DeleteSecurityGroup` / `RevokeSecurityGroupIngress` / `DeleteTags`) require
  `kirocrew:managed=true`, so a leaked launcher credential can't run commands or
  delete resources across the whole account.
- Every user/LLM-influenceable value (`profile`, `region`, `tag`, CIDR) is
  charset-validated (`validation.FieldSpec`) before it reaches subprocess argv;
  the optional SSH CIDR is refused wider than /16 (use your own IP/32).
- **IAM escalation primitives constrained** — `iam:AttachRolePolicy` is pinned
  by `iam:PolicyARN` to exactly `AmazonSSMManagedInstanceCore` (so
  `AdministratorAccess` can't be attached to a `kirocrew-ec2-*` role and passed
  to EC2), and `ec2:CreateTags` is gated by `ec2:CreateAction`
  (`RunInstances`/`CreateSecurityGroup`) so existing resources can't be
  retagged `kirocrew:managed=true` to subvert the tag-gated destructive verbs.
  Validated with AWS Access Analyzer (`validate-policy`): 0 ERROR / 0
  SECURITY_WARNING.

## Deferred hardening (needs live-deploy validation before shipping)

These were surfaced by review and are intentionally NOT shipped blind, because
each is a load-bearing network/permission change that could break the box's
bring-up (the WaitCondition-timeout failure class) if wrong. Each must be
validated against a real `kirocrew cloud launch` on the dev account first.

- **DONE — `iam:PutRolePolicy` → required permissions boundary.** Shipped: the
  template creates a `kirocrew-ec2-boundary-*` managed policy (SSM-core action
  set + source read) and sets it as the InstanceRole's `PermissionsBoundary`;
  the launcher policy gates `iam:CreateRole` on `ArnLike iam:PermissionsBoundary
  == kirocrew-ec2-boundary-*` (the enforcement point; `PutRolePolicy` is a
  separate role-ARN-scoped statement — a boundary set at CreateRole can't be
  removed by PutRolePolicy). Live-validated (role creates, instance SSM Online)
  + IAM policy simulator (CreateRole allow-with-boundary / deny-without) +
  Access Analyzer. NB: the initial cut used `StringEquals`, which does literal
  matching and would have denied CreateRole under the generated policy — a
  reminder that admin-cred launches don't exercise the generated policy's
  conditions; use the policy simulator for condition correctness.
- **DONE — Boundary self-authorship (ceiling is now immutable + pre-created).**
  The boundary is a **single, shared, content-fixed** managed policy named
  `kirocrew-ec2-boundary` (NO per-`StackTag` suffix), created **once** by launcher
  CODE (`source.ensure_instance_boundary`), create-if-not-exists and **never
  re-versioned**. Its content = the exact `AmazonSSMManagedInstanceCore` action
  set + `s3:GetObject` on `kirocrew-src-<account>-*/*` (region-agnostic — IAM is
  global; safe because a boundary only *caps* and the role's inline
  `SourceObjectRead` still pins the single derived object). The template no longer
  creates a boundary; `InstanceRole` references it by a FIXED ARN via a new
  `PermissionsBoundaryArn` parameter, and the launcher policy grants only
  `iam:CreatePolicy` + `iam:GetPolicy` on the **exact** boundary ARN
  (`IamInstanceBoundaryCreateOnce`) — NO `CreatePolicyVersion`/`DeletePolicyVersion`/
  `DeletePolicy`. Crux: `CreatePolicy` on a fixed name fails `EntityAlreadyExists`
  once it exists, and with no version/delete verb a **leaked launcher credential
  cannot make an existing boundary permissive** — so the ceiling now holds against
  a leaked launcher credential, not just the on-box agent. `iam:CreateRole` still
  requires the boundary via `ArnLike iam:PermissionsBoundary`. The agent deny-list
  also blocks `aws iam create-policy`/`create-policy-version`.
  Live-validated on the dev account (account 814959995281, us-east-1): the shared
  boundary was created; a real `kirocrew cloud launch` created a `kirocrew-ec2-*`
  InstanceRole **with** the boundary attached; the instance registered with SSM
  (`describe-instance-information` → PingStatus **Online**), proving the shared
  action set still permits SSM-core; then destroyed cleanly. Also: `cloudformation
  validate-template` passed, Access Analyzer `validate-policy` on the launcher
  policy returned 0 ERROR / 0 SECURITY_WARNING, and `iam simulate-custom-policy`
  confirmed CreateRole is allowed **with** the boundary and denied **without** it
  (`ArnLike`, not `StringEquals`).
  - **Content verification on reuse (closes the first-write-race ESCALATION).**
    `ensure_instance_boundary` no longer trusts an existing `kirocrew-ec2-boundary`
    by name alone: when the policy already exists it fetches the default version
    (`iam:GetPolicy` → `DefaultVersionId`, then `iam:GetPolicyVersion`) and
    compares it — canonically (sorted-key JSON, order-insensitive) — to the
    expected `iam.boundary_policy_document(account)`, and **fails closed on any
    mismatch** (both the normal reuse path and the concurrent-create
    `EntityAlreadyExists` path). So a *permissive* boundary seeded at this name (by
    an attacker who won the create race, or a hand-created one) is **detected and
    refused**, never silently reused to cap nothing. The launcher policy grants
    `iam:GetPolicyVersion` (read-only) alongside `CreatePolicy`/`GetPolicy` for
    this. Live-verified: `ensure_instance_boundary` against the real dev-account
    boundary read its content and confirmed a byte-for-byte (canonical) match.
  - **Residual — first-write race is now DoS-only (honest scope).** With content
    verification, the first-write race can no longer cause an *escalation* (a
    mismatched boundary is refused, never used to under-cap a role). What remains
    is *availability*: an attacker who seeds a non-matching boundary at the fixed
    name before the first legitimate launch would make launches fail the content
    check until it's removed. To eliminate even that, an admin runs the one-time
    **`kirocrew cloud iam-boundary`** to pre-create the (verified, immutable)
    boundary, then removes the `IamInstanceBoundaryCreateOnce` statement from the
    applied launcher policy — after which the launcher only *references* the
    boundary ARN with no `iam:CreatePolicy` grant at all.
- **DONE — `iam:PutRolePolicy`/`PassRole` tag-gated (was name-prefix only).**
  Both now additionally require `aws:ResourceTag/kirocrew:managed=true` on the
  target role, so a leaked launcher credential can no longer target a
  *pre-existing, out-of-band, unbounded* `kirocrew-ec2-*` role (created by a third
  party without our boundary), inline admin, and pass it to EC2. Non-spoofable:
  only a role WE created via the boundary-gated `CreateRole` — which applies
  `Tags` **atomically** at creation (see the template's `InstanceRole.Tags`) —
  carries the tag, and it lands in the same call, so there is **no untagged
  window** before CFN's subsequent `PutRolePolicy`.
  **Tag-timing validated (the failure mode the plan warned about):** a live
  `kirocrew cloud launch` on the dev account produced an `InstanceRole`
  (`kirocrew-ec2-kc-b2a5b9`) that carried `kirocrew:managed=true` **and** the
  inline `SourceObjectRead` policy **and** the shared boundary all coexisting —
  i.e. the role was tagged before/with the inline-policy write, so the tag gate
  does NOT deny the legitimate deploy. Condition correctness confirmed with `iam
  simulate-custom-policy`: `PutRolePolicy` and `PassRole` are **allowed** with the
  managed tag, **implicitDeny** without it (and `PassRole` also denies a non-EC2
  `iam:PassedToService`). Access Analyzer stayed 0 ERROR / 0 SECURITY_WARNING.
  (NB: an *admin*-cred launch does not itself exercise the generated policy's
  conditions — CFN uses the operator's creds — so the simulator is the
  condition-correctness oracle and the live launch proves the tag/inline
  create-order.)
- **DONE — `iam:TagRole` gated so the tag-gate above can't be self-defeated.**
  The `aws:ResourceTag/kirocrew:managed=true` gate on PutRolePolicy/PassRole is
  only non-spoofable if the *same* launcher policy can't apply that tag to an
  arbitrary role. The policy also grants `iam:TagRole` on `kirocrew-ec2-*` (needed
  because CloudFormation's `CreateRole` passes the role's `Tags` inline, which AWS
  authorizes as `iam:TagRole` — id_tags_roles.html), and it was **unconditioned**,
  so a leaked credential could tag a pre-existing unbounded `kirocrew-ec2-*` role
  `kirocrew:managed=true`, then inline admin + pass it — defeating the gate.
  Fixed: `iam:TagRole` is now its own statement (`IamTagRoleOnManaged`) gated on
  `aws:ResourceTag/kirocrew:managed=true`, removed from `IamRoleForInstance`.
  **Validated with a least-privilege assumed-role harness** (a throwaway role
  carrying *exactly* the generated policy — the only valid test, since admin
  bypasses the conditions):
  - (a) least-priv boundary-gated `CreateRole` WITH inline `--tags
    kirocrew:managed=true` → **ALLOWED**. At CreateRole, AWS evaluates the
    embedded TagRole authorization with `aws:ResourceTag` reflecting the tags
    being applied, so the key reads `true` in context → match. The deploy is not
    broken.
  - (b) standalone `tag-role` on a pre-existing **unbounded** victim role (no
    managed tag) → **DENIED**; the full chain `TagRole→PutRolePolicy→PassRole` is
    denied at the first step (verified end-to-end: the victim role stayed clean).
  - Plan-B checks in the same harness: a **boundary-gated** TagRole
    (`ArnLike iam:PermissionsBoundary`) DENIES case (a) — AWS does **not**
    propagate `iam:PermissionsBoundary` into the CreateRole-embedded TagRole check
    — and `aws:RequestTag`/`aws:TagKeys` gates leave (b) open (the attacker sets
    the same tag/keys). So `aws:ResourceTag/kirocrew:managed=true` is the unique
    condition that passes (a) and denies (b). Access Analyzer stayed 0 ERROR / 0
    SECURITY_WARNING; a live launch created the role tagged+bounded+inline-policy'd.
- **DONE — `last_tag` persisted only after a successful deploy.** Previously
  `cloud.json`'s `last_tag` was saved *before* `_deploy_with_progress`, so a
  failed first launch left it pointing at a `ROLLBACK_COMPLETE`/no-instance stack
  and the next `launch` resumed that broken stack and aborted at "instance not
  ready" instead of retrying clean. Now `cfg.save()` runs only after the deploy
  confirms healthy; `_saved_launch_is_usable` additionally ignores a stale saved
  tag (from an older build) whose stack is in a failed terminal state or has no
  instance. (Regression tests: failed launch persists nothing; success persists;
  stale failed tag is not resumed.)
- **DONE — social-login tunnel teardown on url-less paths.** Two MEDIUMs in the
  same tunnel-teardown family as the `connect()` mint fix: `start_device_login`
  now reaps its callback tunnel in-function when the continued social step yields
  no URL (rather than returning a live-tunnel-but-url-less prompt), and
  `wizard._verify_operational` calls `prompt.close()` on its no-URL return —
  mirroring the `launch()` branch — so no social-login path orphans a
  `session-manager-plugin` child + loopback callback port.
- **DONE — `ec2:RunInstances`/`CreateSecurityGroup` request-tag conditions.**
  Both create verbs now require `aws:RequestTag/kirocrew:managed=true` so a leaked
  launcher credential can't create untagged EC2 instances / security groups that
  escape the tag-gated Stop/Terminate/Delete/Authorize statements. The nasty
  gotcha the deferral flagged is handled by a **per-resource-ARN split** (a
  blanket request-tag on the whole action 403s the launch, because these calls
  authorize per-resource across ARNs that don't carry the tag):
  - `ec2:RunInstances` → `Ec2RunInstancesTaggedInstance` requires the tag on
    `instance/*` only; `Ec2RunInstancesSupportingResources` grants the other ARNs
    the call touches — the `volume/*` + `network-interface/*` it creates as
    sub-resources (this template's `TagSpecifications` tags only the **instance**,
    not the volume/ENI) and the referenced `image/*`/`subnet/*`/`security-group/*`/
    `key-pair/*` — WITHOUT the condition.
  - `ec2:CreateSecurityGroup` → `Ec2CreateSecurityGroupTagged` requires the tag on
    the new `security-group/*`; `Ec2CreateSecurityGroupVpc` grants the referenced
    `vpc/*` unconditioned.
  Validated with a **least-privilege assumed-role `--dry-run` harness** (full
  per-resource IAM authorization without creating anything; admin launches bypass
  the policy so this is the oracle): RunInstances with the managed request-tag on
  the instance → **AUTHORIZED** (incl. the template-shaped call with explicit
  `NetworkInterfaces`/`AssociatePublicIpAddress` + gp3-encrypted `BlockDeviceMappings`
  + IMDSv2 opts, volume/ENI untagged); RunInstances with an untagged instance →
  **DENIED** on `instance/*`; CreateSecurityGroup tagged → **AUTHORIZED**, untagged
  → **DENIED** on `security-group/*`. Access Analyzer stayed 0 ERROR / 0
  SECURITY_WARNING. (`aws:TagKeys` was NOT added — the per-resource-ARN split
  already scopes creation to tagged instance/SG, and a `ForAllValues:aws:TagKeys`
  restriction risks denying CFN's own propagated stack tags.)
- **Narrow the instance boundary's `ssm:GetParameter`.** The boundary mirrors
  `AmazonSSMManagedInstanceCore`, which grants `ssm:GetParameter*` on `*`; scope
  it to the paths the SSM agent actually needs (`/aws/service/*` patch baselines)
  so a prompt-driven agent on the box can't read arbitrary account parameters.
  Deferred: over-narrowing risks breaking the SSM agent's own reads (WaitCondition
  timeout) — needs live validation.
- **On-box IMDS reachability firewall for the gateway user.** IMDSv2 + hop-limit
  1 already blunt SSRF, but a defense-in-depth `IPAddressDeny`/iptables rule
  blocking the ec2-user gateway process from `169.254.169.254` would fully wall
  off the instance-role STS creds. Deferred because it's a network change that
  could break the SSM agent's own IMDS use if scoped wrong.
