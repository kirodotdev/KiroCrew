# Reaching a cloud-launched instance: native SSM vs legacy SSH

When the cloud launcher provisions an EC2 box for you, it also registers that box
in the **Instances** registry so the dashboard's **Settings → Instances** page can
manage it — open a tunnel, mint a short-lived dashboard token, keep it warm, and
self-heal a dropped connection. This page explains **how** that managed connection
is made, and why the launcher now uses the **native SSM transport** instead of the
older SSH-over-`ProxyCommand` workaround.

> TL;DR — a launched box is now reachable through Instances with **no SSH key, no
> inbound port, and no `~/.ssh/config` edits**. Reachability is an IAM decision
> (grant/revoke `ssm:StartSession`, CloudTrail-audited), not a networking decision
> gated by a key someone holds.

## The two transports

The Instances registry supports two `connection_method` values. Both end at the
same place — a loopback-bound gateway on the remote — but they get there very
differently.

### Native SSM (`connection_method="ssm"`) — the new default for launched boxes

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
only the `aws` CLI + `session-manager-plugin` and `ssm:StartSession` permission.

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

| | **Native SSM** (new) | **Legacy SSH-over-ProxyCommand** |
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

## What you see in Settings → Instances

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
cleaned up too.

## Related

- `docs/system-specs/modules/cloud.md` — the cloud launcher module (provisioning,
  security model, bootstrappers).
- `docs/system-specs/modules/instances.md` — the Instances registry and the two
  transports; §9 documents the manual SSH-over-`ProxyCommand` option.
