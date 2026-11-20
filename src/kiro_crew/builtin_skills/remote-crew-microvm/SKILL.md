---
name: remote-crew-microvm
description: "Drive the AWS Lambda MicroVM remote-crew lane: check whether it is configured, hand the operator the cloud.json block when it is not, launch a crew, reach it, and tear it down. Use for microvm, lambda microvm, remote crew, cheap cloud crew."
always: false
triggers: microvm, micro vm, lambda microvm, remote crew, cloud crew, cloud.json, remote provisioner, launch a crew in the cloud
inject_on_trigger: false
---
# Running a remote crew on an AWS Lambda MicroVM

A crew on this lane runs inside a MicroVM that the platform terminates at a
maximum lifetime it will not extend. That clock is the whole shape of the lane, and
almost every surprise on it comes from the one fact below.

**The crew's home lives on the VM's disk and goes away with the VM.** Nothing on
this lane copies it anywhere: no suspend, no archive, no reopen. When the VM goes,
the conversations go. Say that plainly to anyone who asks what a crew here keeps,
and say it BEFORE they put work into one.

**Before anything else: the lane is offered only when its block is complete.**

An incomplete `microvm` block publishes no provisioner row at all, which is the
deliberate choice: a row whose every launch is refused is worse than no row. So
the first thing to do is READ the provisioner list, not assume. If the `microvm`
row is absent, the block is incomplete and the missing field is the thing to find
-- most often `activation_role_arn`, which the AWS call that enrols the crew
refuses to run without, or `bundle_dir`, which a block that will build an image
also requires.

Completeness is judged on every request, so a correct edit shows up immediately
with no restart.

**Two numbers that decide everything.**

| | |
|---|---|
| **1 hour** | the lifetime a launch asks for when the operator sets none. Raisable through `wall_seconds`; it is this short because the home is not kept past it |
| **28,800 seconds** | the platform's own maximum, and the ceiling on `wall_seconds`. Not adjustable and nothing extends it |

## First: is the lane even configured?

Do this before anything else, and do not infer it from the presence of an AWS
profile. The lane is offered only when its configuration is **complete**; an
incomplete block leaves it unregistered rather than registered and refusing.

```
GET /api/cloud/provisioners
```

Look for a row with `"id": "microvm"`. Three outcomes, and they mean different
things:

- **the row is there** — the lane is offered and configured. Note its
  `confirm_before_launch` string exactly as written; you need it verbatim to launch.
- **no `microvm` row, but `aws_ec2` is there** — this is the expected answer today.
  The lane is withheld until its guest half lands, and it is also what an
  incomplete block looks like. Do not try to tell the two apart by guessing: report
  that the lane is not available yet, and only walk through the configuration below
  if the operator asks for it knowing it will not launch.
- **the request fails** — this is not a lane problem. Report it as it is.

## The operator has to write the config; you cannot

The cloud configuration file is the **operator's** file. The product only reads it,
and the agent file-edit tool refuses it: a value in it chooses what image runs and
where the crew's home is written. So your job is to hand them something they can
paste without thinking, and then verify it took.

Say this, filling in nothing you have to guess:

> Two steps, both yours because the file is sealed against my edits.
>
> **1. Deploy the base stack once per account and region.** It holds the key each
> crew's control secret is encrypted under, the bucket an image build reads its
> recipe from, and the roles the guest registers and runs under. All of them outlive
> every crew that uses them, and the lane never creates any of them — a resource
> whose loss is unrecoverable is created deliberately, not as a side effect of a
> launch. The template ships with Kiro Crew as `kirocrew-microvm-base.yaml`; deploy
> it with `aws cloudformation deploy` and keep four outputs: `LaneKmsKeyArn`,
> `RecipeBucketName`, `ImageBuildRoleArn` and `HybridActivationRoleArn`.
>
> **2. Put the crew's model credential in Secrets Manager.** A crew refuses to
> serve without one, so this is a prerequisite and not a later step. Its value is
> a Kiro identity document, not an API key.
>
> The operator chooses the path and then names it in the config as
> `identity_secret_ref`. It is configured rather than derived from the crew's
> launch tag, because that tag is not theirs to choose: a launch with no tag is
> given `kc-<random>`, so a path built from it cannot exist before the launch that
> invents it, and the crew would stop at its secrets stage while the VM billed to
> its wall. Give the operator the exact path they will paste back; never pass a
> credential value through a command line, a log or a config file.
>
> **3. Add this block to `~/.kiro/crew/cloud.json`**, filling in the six values:
>
> ```json
> {
>   "microvm": {
>     "base_image_arn": "<the AWS-managed MicroVM base image ARN>",
>     "build_role_arn": "<ImageBuildRoleArn>",
>     "recipe_bucket": "<RecipeBucketName>",
>     "bundle_dir": "<the directory packaging.build produced>",
>     "kms_key_id": "<LaneKmsKeyArn>",
>     "activation_role_arn": "<HybridActivationRoleArn>"
>   }
> }
> ```
>
> `bundle_dir` is the crew bundle `packaging.build` produced -- the SAME artifact the
> Fargate lane builds its crew layer from. It is required on the build path and
> ignored when `image_identifier` pins a prebuilt image.
>
> Identifiers only -- nothing in the block is a secret, and nothing in the product writes
> this file.

Then **verify rather than assume**: re-read the provisioner list. The block is
judged on every request, so a correct edit shows up immediately with no restart,
and an incomplete one still shows no `microvm` row. If the row is still absent
after they say they saved it, the block is incomplete; the most common cause is a
missing `activation_role_arn`, which the lane requires because the AWS call that
enrolls the crew refuses to run without a role; the next most common is a missing
`bundle_dir`, which a block that will build an image also requires.

**The recipe is the Fargate lane's recipe, not a second one.** If asked what goes
in the zip: `Dockerfile.crew` read from disk, plus the bundle's own files at the
zip's root -- byte-for-byte the build context `docker build` gets on that lane.
Same Dockerfile, same required members (`manifest.json`, `agent.json`, `mcp.json`,
`skills/`), same bundle digest from `packaging.build`'s own function. Do not
suggest writing a MicroVM-specific Dockerfile or bundle layout: two answers to
"what is in a crew image" drift, and the looser one ships the wrong content.

A bundle short a member is refused at preflight, by name, rather than minutes into
a build whose log the operator does not hold.

**The crew's image is built, not chosen — say this if they ask for an image id.**
`base_image_arn` is a BASE. The lane builds the crew's image from that base plus
the crew's own signed bundle, the way the Fargate lane builds a task definition
from the same two inputs. The build runs on AWS from a recipe zip; nothing is built
on anyone's machine.

The practical consequence, and the one you will be asked about: **the first launch
after the crew's bundle changes takes minutes, and the next one does not.** An
image is cached by a digest of its two inputs, so an unchanged crew reuses the
image it already has. Do not read a multi-minute first launch as a stuck one, and
do not relaunch to "clear" it — a second build costs the owner a second build.

An image can also exist and still not be launchable: a build that finished and
produced no active version is rebuilt rather than reused, because the service
refuses a launch against an image with no active version. If an operator insists a
built image is there and the lane is building anyway, that is why.

## Launching

```
POST /api/cloud/launch
{"provider_id": "microvm", "confirm_recipient": "<the confirm_before_launch string, verbatim>"}
```

`confirm_recipient` must equal the `confirm_before_launch` value the provisioner
list published, character for character. It names the image and the lane's key
in full, deliberately rather than as a fingerprint: it exists so a person reads it
and recognises a wrong one, so **show it to the operator and let them confirm it**
rather than copying it through silently. A mismatch answers 400 and names what it
would have used.

The lane is reachable through this route and **absent from the Set-up selector**,
because no form is drawn for it yet. That is expected, not broken. If someone asks
why they cannot find it in the UI, that is the answer.

The launch runs four steps and the one that takes time is the third: the crew has
to enroll itself as a managed node before anything can reach it. Do not read a
pending launch as a stuck one for at least a couple of minutes.

Once it is up the crew is an ordinary remote instance. The Instances pane,
federated session search, and running a session's turns on the remote peer all
work with no extra step.

## Reading what a crew is doing

A crew on this lane is in one of these, and the distinctions are the point:

| State | What it means for the owner |
|---|---|
| `pending` | launching; nothing can reach it yet |
| `running` | usable |
| `terminated` | the VM is gone, and the crew's home went with it |
| `launch_failed` | the launch itself could not be completed |
| `unknown` | nobody has checked recently enough to say. This is never stored; it means the control plane has not looked, not that the crew is broken |

One of those deserves care in how you report it:

- **`unknown` is not a failure.** It is the honest answer when the last
  observation is stale, which happens whenever the gateway's host was asleep.
  Do not offer to open a crew whose state is `unknown`; check it first.

## Connecting to a crew

Nothing special, and that is the design. A crew on this lane registers as an
ordinary remote instance over AWS SSM, against the `mi-` managed node its guest
enrols as, so everything downstream already works: the Instances pane, **Settings
-> Remote Crew**, federated session search, and running a session's turns on the
remote peer.

So do not look for a MicroVM-specific way to reach a crew. If you can reach a
remote instance, you can reach this one; if you cannot, the problem is the
instances layer and not this lane.

One constraint that bites people: a session's remote binding happens **only at
birth**. You cannot convert an existing local session to run on a remote crew --
its transcript would stay where it is while execution moved to an empty slot on
the peer. Make a new session bound to the crew instead.

## Tearing a crew down

A teardown terminates the crew's VM and deletes the SSM activation it enrolled
with, and the record ends at `terminated`.

**It is the end of that crew's conversations.** The home was on the VM's disk and
nothing copied it off, so say that before anyone tears one down, not after.

## The cost guard

The lever is the VM's **maximum lifetime**, and only one of the two numbers is
yours:

- **28,800 seconds is the platform's maximum** and it is not adjustable;
- **`wall_seconds` in the config block sets a shorter one**, and the lane asks for
  one hour when it is unset.

The honest framing: `wall_seconds` is a ceiling on the damage rather than a budget.
A crew bills while it runs, and the lifetime is what stops a forgotten crew billing
all day. It is also how much conversation the owner loses at the edge, because the
crew is terminated there whatever it is doing. Those two pull in opposite
directions, which is why the default is an hour rather than the maximum.

## What not to do

- **Do not try to edit the cloud configuration file.** It is sealed, the refusal
  is correct, and the fix is to hand the operator the block above.
- **Do not launch to "see if it works".** A MicroVM bills from the moment it
  starts and the lifetime clock does not stop for a crew nobody wanted.
- **Do not describe a crew on this lane as keeping anything.** No suspend, no
  archive, no reopen: a crew is gone when its VM is. Offering to "reopen" one is
  offering something that does not exist.
