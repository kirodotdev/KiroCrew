# Reaching a cloud-launched instance: native SSM vs legacy SSH

When the cloud launcher provisions an EC2 box for you, it also registers that box
in the **Instances** registry so the dashboard's **Settings → Remote Crew** page can
manage it — open a tunnel, mint a short-lived dashboard token, keep it warm, and
self-heal a dropped connection. This page explains **how** that managed connection
is made, and why the launcher uses the **native SSM transport** instead of the
older SSH-over-`ProxyCommand` workaround.

> TL;DR — a launched box is reachable through Instances with **no SSH key, no
> inbound port, and no `~/.ssh/config` edits**. Reachability is an IAM decision
> (grant/revoke `ssm:StartSession`, CloudTrail-audited), not a networking decision
> gated by a key someone holds.

## The two EC2 connection methods

The Instances registry supports three `connection_method` values overall:
`ssh`, `ssm`, and `fargate`. An EC2 box uses the first two; `fargate` is reserved
for ECS task targets and is outside this comparison. Both EC2 methods end at the
same place — a loopback-bound gateway on the remote — but they get there very
differently.

### Native SSM (`connection_method="ssm"`) — the default for launched boxes

The gateway builds an `aws ssm start-session` port-forward directly
(`AWS-StartPortForwardingSession`) and mints the dashboard token over
`aws ssm send-command`. No SSH is involved at any layer.

A launched box is registered like this (`cloud/connect.py::register_instance`):

```python
reg.add(
    name="Kiro Crew Cloud (kc-3f9a)",
    connection_method="ssm",
    ssm_target="i-0abc123456789def0",   # the EC2 instance id
    aws_profile="dev",                   # the profile the launch used
    aws_region="us-west-2",
    remote_port=5476,
)
```

Requirements on the remote: only the **SSM agent** (preinstalled on Amazon Linux)
and an instance role that permits Session Manager. Requirements on the client:
the `aws` CLI + `session-manager-plugin`; `ssm:StartSession` opens the tunnel,
while managed token and remote-command operations also require
`ssm:SendCommand` and `ssm:GetCommandInvocation`. The policy printed by
`kirocrew cloud iam-policy` includes these permissions with the launcher-managed
resource bounds.

### Legacy SSH over an SSM `ProxyCommand` (`connection_method="ssh"`)

Before the native transport existed, an SSM-only box was reached by registering
the instance id as `ssh_host` and having the operator hand-edit `~/.ssh/config`
with a `ProxyCommand` that wraps `aws ssm start-session` (see
`docs/system-specs/modules/instances.md` §9). The gateway then runs a normal
`ssh -N -L` through that proxy.

This still works and is still a valid manual option, but for a launched box it
means the "one command" provisioning silently depends on **three things the
launcher does not manage**: `sshd` running on the box, an SSH key on the client,
and a correct `~/.ssh/config` entry. Under the standard hardened posture for an
SSM-managed instance (zero SSH ingress, no distributed key) that path cannot
connect at all.

## Side-by-side

| | **Native SSM** (default) | **Legacy SSH-over-ProxyCommand** |
|---|---|---|
| `connection_method` | `ssm` | `ssh` |
| Registry field for the target | `ssm_target` (`i-…`/`mi-…`) | `ssh_host` (instance id, resolved by ssh config) |
| Inbound SSH port on the box | **none** | required (`sshd` reachable through the proxy) |
| SSH key on the client | **none** | required |
| `~/.ssh/config` editing | **none** | required (hand-written `ProxyCommand`) |
| `sshd` on the remote | not needed | required |
| Reachability controlled by | IAM (`ssm:StartSession`), CloudTrail-audited | possession of an SSH key + local ssh config |
| Works under zero-ingress hardening | **yes** | no |
| Extra AWS fields used | `aws_profile`, `aws_region` (optional) | — |
| Managed features (tunnel, token refresh, warm-set, self-heal) | identical | identical |

Both transports share the *same* state machine — health probe, two-tier
self-heal, proactive token refresh, stored-token liveness probe, and startup
auto-revive are transport-agnostic. SSM is a second transport plugged into that
machine, not a parallel implementation; the native SSM tunnel even reuses the
cloud module's own `cloud/ssm.py` primitives (`build_port_forward_argv`,
`run_command`).

## Launching a box (one command)

The launcher provisions the EC2 box, installs and starts the gateway (bound to
loopback `:5476`, never public), signs kiro-cli in over SSM, registers the box
here, and opens the dashboard — all from your laptop.

1. **One-time prep.** Install the AWS CLI and `session-manager-plugin` — on a bare
   machine the `cloud-install.sh` (macOS/Linux) or `install.ps1` (Windows)
   bootstrapper does this and hands off to the wizard. Verify prerequisites with
   `kirocrew cloud doctor`. An admin creates the least-privilege policy printed
   by `kirocrew cloud iam-policy` as a customer-managed policy and attaches it to
   the IAM user, group or role behind the AWS profile you'll launch with: see
   [Applying the launcher policy](#applying-the-launcher-policy).
2. **Launch.** Run `kirocrew cloud launch` — interactive (size picker + a confirm
   that names the AWS account and profile the stack will be created in),
   or non-interactive, e.g. `kirocrew cloud launch --size power --region us-west-2
   --profile dev -y`. Useful flags: `--new` (a separate box instead of resuming
   your saved one) and `--keep-on-failure` (disable CloudFormation rollback to
   inspect a failed bootstrap). To customize a **new** box: `--extra-packages
   gh,jq` installs extra dnf packages after the stock set (a name dnf cannot
   find is skipped with a warning in `/var/log/kirocrew-setup.log`; names are
   letters, digits and `. _ + -`, at most 256 characters in total), and
   `--ami ami-xxxx` launches from your own image instead of the latest Amazon
   Linux 2023 one (it must match the size tier's architecture and be
   AL2023-compatible, since the bootstrap still runs `dnf`). Before it asks to
   create the stack, `launch` reads the image's architecture and refuses one
   that does not match the size, naming the sizes that fit it (when that read
   fails, the stack still fails at instance creation instead); an image without
   `dnf` or `cfn-signal` still fails or times out during the bootstrap. Both
   are fixed when the instance is created: with an existing saved box, use
   `--new`.
3. **Reach it — and the ports.** Launch ends by opening an SSM port-forward and
   your browser at `http://localhost:<local>/?token=…`. You never choose the
   **remote** port: the gateway always listens on loopback `:5476` on the box. The
   **local** forwarded port defaults to `5599` (deliberately not `5476`, so it
   won't collide with a local gateway you may run yourself); override it with
   `--local-port` on the `connect` / `tunnel` verbs, or suppress the browser with
   `--no-browser`. Because the box is registered here as an SSM crew, the Instances
   hub afterwards opens its **own** managed SSM tunnel — allocating its own local
   port, with token refresh and startup auto-reconnect — independent of that
   one-shot launch tunnel. Re-running `launch` for the same box updates its record
   in place; `kirocrew cloud destroy` removes both the box and this registration.

For setting up a box you manage yourself (SSH or SSM, and the common EC2 gotchas),
see [remote-crew-on-ec2.md](remote-crew-on-ec2.md).

## Applying the launcher policy

`kirocrew cloud iam-policy` prints one IAM policy document on stdout and nothing
else, so it pipes straight into the AWS CLI. An IAM admin applies it once.

**Create it as a customer-managed policy, not an inline one.** The policy is
close to IAM's 6,144-character limit for a managed policy, so it is far over the
2,048-character limit for a user's inline policies: pasted as an inline policy on
an IAM user, it cannot be saved. Run these with an admin profile:

```bash
kirocrew cloud iam-policy > launcher-policy.json
aws iam create-policy --policy-name kirocrew-launcher \
  --policy-document file://launcher-policy.json --profile <admin>
```

**Attach it to the identity the launch profile signs in as.** IAM attaches a
policy to a user, group or role, never to a CLI profile, so pick the one behind
the profile you pass to `kirocrew cloud launch --profile`:

| The launch profile uses | Attach with |
|---|---|
| an IAM user's access keys | `aws iam attach-user-policy --user-name <user> --policy-arn arn:aws:iam::<account>:policy/kirocrew-launcher` |
| an IAM group that user belongs to | `aws iam attach-group-policy --group-name <group> --policy-arn …` |
| a role it assumes (`role_arn` in `~/.aws/config`) | `aws iam attach-role-policy --role-name <role> --policy-arn …` |
| IAM Identity Center (`sso_session` / `sso_start_url`) | add `kirocrew-launcher` to the permission set as a customer managed policy reference; the policy must exist under that name in each account the permission set is assigned to |

When the plain form also prints a line on stderr naming
`kirocrew cloud iam-policy --host-policies`, this build attaches extra host
policies to each instance role: create and attach that second document the same
way, beside the first.

**What it refuses on purpose.** A launch that fails with an access-denied error
is not always a missing grant. The policy carries three explicit `Deny`
statements, which no other `Allow` on the same identity can undo:

| Statement | What it refuses | Why |
|---|---|---|
| `DenyStartSessionOutsideTheLane` | `ssm:StartSession` on anything except the port-forward document `AWS-StartPortForwardingSession`, EC2 instances, the `kirocrew-crew-*` Fargate tasks and SSM sessions | the launcher only ever opens port-forwards, so an interactive shell session is refused, including through a document AWS adds later |
| `DenyForeignUserData` | `ec2:DescribeInstanceAttribute` on any instance not tagged `kirocrew:managed=true` | user data often holds bootstrap secrets |
| `DenySecretReads` | `ec2:DescribeLaunchTemplateVersions`, `ec2:DescribeSpot*Requests`, `ec2:DescribeTags`, `ec2:DescribeVpnConnections` | they return user data, VPN pre-shared keys or every tag value in the account, and the launcher uses none of them |

It also leaves out, without a `Deny`, calls the launcher never makes:
`ecs:ExecuteCommand`, `ssm:TerminateSession`, `ssm:ResumeSession` and
`ssm:ListCommandInvocations`.

**Optional: pre-create the boundaries once.** Each launch runs its instance and
crew roles under an immutable permissions boundary, and the policy's
`IamInstanceBoundaryCreateOnce` statement lets the first launch create the one it
needs. To take that create right away from the launcher, an admin runs
`kirocrew cloud iam-boundary --profile <admin>` once. It creates
`kirocrew-ec2-boundary`, `kirocrew-crew-boundary` and
`kirocrew-crew-exec-boundary` (leaving any that already match alone), after
which you may remove the `iam:CreatePolicy` action from that statement and save
the edited document as a new default version
(`aws iam create-policy-version --set-as-default`). Keep the statement's
`iam:GetPolicy` and `iam:GetPolicyVersion`: every launch reads its boundary to
check it.

## What you see in Settings → Remote Crew

A launched box appears as a managed instance whose connection line reads, e.g.:

```
Kiro Crew Cloud (kc-3f9a)
SSM  i-0abc123456789def0 (us-west-2)   port 5476   TTL 20h · token 19h58m left
```

The `SSM` tag (vs `SSH`) is the transport; for an SSM instance the target shown is
the `ssm_target` and its region, with no SSH host or key anywhere in the flow.
**Connect** opens the SSM port-forward and mints a token; **Diagnose** runs the
SSM-aware health probe.

## Removing a launched box

`cloud destroy` deletes the AWS stack and then unregisters the instance.
`unregister_instance` matches the box by `ssm_target` (native registration), and
still falls back to matching `ssh_host` so a box registered the legacy way is
cleaned up too. It removes the box together with every crew chained through it
(`remove_cascade`, the same helper the dashboard's delete uses), because a chained
crew rides the box's forward and cannot connect without it.

## Related

- `docs/system-specs/modules/cloud.md` — the cloud launcher module (provisioning,
  security model, bootstrappers).
- `docs/system-specs/modules/instances.md` — the Instances registry and all three
  connection methods; §9 documents the manual SSH-over-`ProxyCommand` option.
