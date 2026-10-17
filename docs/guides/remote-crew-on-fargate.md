# Setting up Remote Crew on Fargate

You run a crew as an AWS Fargate task instead of on an EC2 box you keep alive.
The lane is **partly shipped**: the launch engine exists and the dashboard's
provisioner API can create a task, but there is **no wizard step, no dashboard
form, and no `cloud` CLI verb** for it yet. The one supported way to configure it
today is to **hand-edit a `fargate` block into `~/.kiro/crew/cloud.json`**.

This page is what you read **before** launching, so a legitimate flow does not
look unavailable. For the design and the exact refusal rules see
[`../system-specs/modules/cloud.md`](../system-specs/modules/cloud.md) ("The
Fargate lane's configuration home") and the
[Fargate RFC](../request-for-change/rfc-remote-instance-on-fargate.md).

## Why you edit the file by hand

`cloud.json` is the **operator's** file. The product only ever *reads* it — there
is no `save()`, no `apply_update()`, and it is deliberately **sealed against
agent-assisted file edits** (the agent file-edit tool refuses it and the sandbox
mounts it read-only), because a value in it — the launch tag — is what `kirocrew
cloud destroy` deletes when no `--tag` is given. So the fields below are ones only
you can write, from outside the sandbox, in an editor. There is no product surface
that writes them for you today; adding a `cloud config` command or a dashboard form
is a possible future enhancement, not a bug.

The lane only becomes reachable once the block is **complete** — a half-written
block leaves the lane **unregistered** rather than registered and refusing every
launch, so you never spend attention at launch time on a mistake that was visible
when you saved the file.

## The `fargate` block

Add a top-level `"fargate"` object to `~/.kiro/crew/cloud.json`. Every field below
except the ones marked optional is **required** for the lane to register.

| Field | Required | Value |
|---|---|---|
| `cluster` | yes | the ECS cluster the task runs in |
| `subnets` | yes | list of subnet ids; at least one |
| `security_groups` | yes | list of security-group ids; at least one |
| `image` | yes | the crew container image, **digest-pinned only** — `<repo>@sha256:<64-hex>`; a movable tag is refused |
| `secrets` | yes | list of `[canonical-name, ARN]` pairs; each name has the form `kirocrew/crew/<crew>/<KEY>`, and **one must be your model credential, named `kirocrew/crew/<crew>/KIRO_IDENTITY`**. That secret holds one KasToken JSON document, not an API key. Identifiers only — never a secret value; the task's execution role fetches the value from Secrets Manager at start |
| `cpu_architecture` | no | `X86_64` (default) or `ARM64` |
| `assign_public_ip` | no | JSON boolean; defaults to `false`. A public subnet with no NAT gateway cannot pull the image without this |
| `task_ttl_seconds` | no | JSON integer above zero; how long one task may run before the launcher stops it. Omitted takes the engine's own default |
| `internal_only` | no | JSON boolean; defaults to `false`. **See the security note below** — leaving it `false` is the safe direction |

A malformed block reads as **no Fargate configuration at all**, the same as an
absent one: any missing required field, a movable image tag, a secrets entry that
is not a two-string pair, a non-boolean in a boolean field, or a `task_ttl_seconds`
that is not a positive JSON integer, voids the whole block. The block is read **per
call**, so editing the file takes effect on the next request and deleting the block
removes the lane — no gateway restart.

### Example

```jsonc
{
  "fargate": {
    "cluster": "my-crews",
    "subnets": ["subnet-0abc123", "subnet-0def456"],
    "security_groups": ["sg-0abc123"],
    "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/kirocrew-crew@sha256:<64-hex>",
    "secrets": [["kirocrew/crew/<crew>/KIRO_IDENTITY", "arn:aws:secretsmanager:us-east-1:123456789012:secret:kirocrew/crew/<crew>/KIRO_IDENTITY-AbCdEf"]],
    "cpu_architecture": "X86_64",
    "assign_public_ip": false,
    "task_ttl_seconds": 21600
  }
}
```

## Security note: `internal_only`

`internal_only` is the one key in this block that **loosens** anything, which is
why it lives in the file you own and nothing in the product ever writes it. A crew
container is sandboxed-only: kiro-cli sandboxes the model subprocess in an
unprivileged user namespace, and Fargate cannot provide one — so a Fargate task
that keeps the sandbox exits at startup. Setting `internal_only: true` writes
`SMC_INTERNAL_ONLY` into the task and starts the model subprocess **unsandboxed**,
which is what lets the lane run on Fargate at all.

It does **not** mean "no untrusted input reaches this task." A crew reads untrusted
content in the ordinary course of its work — tool output, a fetched web page, a
connector payload — any of which can carry an injection, and with `internal_only`
set a worker reached that way can read the model credential. It is a statement that
**you run your own crews on your own account and bear that risk**, not a claim that
injection cannot happen. Do not set it for multi-tenant or externally-reachable
crews.

## After the lane registers

Once the block is complete, the `aws_fargate` provisioner lane is offered through
the dashboard's provisioner API. What to expect from it:

- The lane is **not** in the Set-up picker — no frontend renderer claims its
  `kind` yet, so the dashboard skips it in that selector.
- Every `POST /api/cloud/launch` for this lane must carry `confirm_recipient`
  equal to the descriptor's `confirm_before_launch` value, which
  `GET /api/cloud/provisioners` publishes. A missing or different value is
  refused with `400 recipient_not_confirmed` before any task registers. See
  "The credential recipient is confirmed at launch" in
  [`../system-specs/modules/cloud.md`](../system-specs/modules/cloud.md).
- A launch adds the task to the Instances registry on its own, with connection
  method `fargate`. Add it by hand under **Settings → Remote Crew** only when the
  connect step reports that it could not; the launch is then reported as
  launched with that reason, because the task is running and billing either way.
- A launched task keeps its data home on the task's own disk, so its sessions and
  transcripts are lost when the task stops. No persistent store is configurable.

A running crew is then reached through the instances layer's `fargate` connection
method, surfaced as **Settings → Remote Crew**.
