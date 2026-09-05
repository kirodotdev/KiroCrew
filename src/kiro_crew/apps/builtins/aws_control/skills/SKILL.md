---
name: share-my-crew
description: "Operate the Share My Crew app: curate a locally tuned crew deny-by-default, then one-command deploy it into the owner's own AWS account as a private, SigV4-only service. Use when the user wants to package a crew, deploy or redeploy one, resume a failed deploy, diagnose a crashing or empty-answering task, or understand what the verify gates prove."
---

# Share My Crew

Share My Crew takes a crew the owner tuned on their own machine and stands it up
in the owner's own AWS account as a service their customers reach. The crew rides
INSIDE the deployed image: one artifact whose digest pins both the serving code
and the crew content (`crew/PACKAGING-CONTRACT.md`). There is no public endpoint;
every caller authenticates with SigV4 against a private control API
(`crew/scripts/smc-deploy.sh` header).

You do not hold AWS credentials. The operator runs the deploy with their own
profile (`aws sso login --profile <p>`). Your job is to run curation locally,
hand the operator the exact deploy and diagnosis commands, and read the results
back with them.

## The one command and its 8 steps

```
crew/scripts/smc-deploy.sh --profile <p> --region <r> --crew <name>
```

Everything is resolved from `sts` and stack outputs, not the command line. The
step list is in the `crew/scripts/smc-deploy.sh` header and dispatched in `smc_main`:

| Step | What it does |
|------|--------------|
| 0 preflight | `sts get-caller-identity`, assert the route prefix starts with `/`, print the plan, confirm the account. |
| 1 bundle | Run `python -m packaging.build --crew <name> --out <dir>` (track T1), read the `SMC_BUNDLE_JSON=` marker. Produces the four-entry bundle and its digest. |
| 2 base stack | Deploy `smc-base` (VPC, ECS cluster, internal ALB, VPC link, control API, bucket). Always deployed, never skipped for an existing stack. |
| 4 crew image | Run `build_crew_image.sh` (track T2): a thin layer on a digest-pinned base with the bundle baked in, pushed digest-pinned. `--dry-run` skips this step. |
| 5 secrets | Store `KIRO_API_KEY` and the control secret in Secrets Manager. The crew stack receives ARNs, never plaintext. |
| 6 crew stack | Deploy `smc-crew-<name>` (task definition, service, listener rule, control routes). |
| 7 republish stage | `apigateway create-deployment`. CloudFormation cannot express this. |
| 8 verify | The gates. Each can fail; the success banner prints only after all pass. |

Step 3 does not exist. It was the "upload the bundle to S3" step and it is
deleted, not disabled (`crew/PACKAGING-CONTRACT.md`, and the `# --- 3 . (removed)`
block in `crew/scripts/smc-deploy.sh`). Nothing in the container read the S3 bundle, so
the deployment served a default agent while ten gates reported green. Two sources
of truth for "which crew is this" is how that went unnoticed, so the crew now
lives only in the image. Do not reintroduce an S3 bundle path or a `CREW_BUNDLE`
env var.

Useful flags (`smc_main` arg parsing in `crew/scripts/smc-deploy.sh`):

- `--dry-run`: exercise the whole step sequence, state, and resume logic with no
  AWS call, no docker build, no push. `SMC_FIX_*` env vars point a dry run at a
  failing case (for example `SMC_FIX_TASKDEF_IMAGE=` to force the image-digest
  gate to fail), which is how you prove a gate can fail rather than only that it
  passes.
- `--base <repo>@sha256:<hex>`: pin the base image. When absent it is resolved
  from the ECR repo's `smc-base` tag.
- `--allow <path>`: a signed curation plan (see below). Repeatable.
- `--arch <arm64|amd64>`, `--cpu`, `--memory`, `--retention`, `--stage`.
- `--require-sandbox`: demand the kiro-cli sandbox. On Fargate this makes the
  container refuse to start, correctly and loudly (`crew/runtime/container/CONTRACT.md`).

## Curation is deny-by-default

Nothing from the owner's machine ships until it is explicitly reviewed, signed,
and passed to the build. The owner's local crew holds credentials, private skills,
and internal MCP servers that must never reach a customer, so every skill and MCP
server starts excluded (`crew/packaging/build.py`, `skill_candidates` and
`mcp_candidates`: "everything starts excluded").

Two commands, one for review and one to build. Both run FROM `crew/`, because
`packaging` is resolved as a top-level module from the working directory -- the
same cwd the driver uses (`cd "$CREW_ROOT" && "$py" -m packaging.build`). Run them
anywhere else and the PyPA `packaging` distribution answers instead, with no
`build` submodule.

```
# 1. Write the review template and print what would ship (writes NO bundle):
python -m packaging.build plan  --crew <name> --out <dir>

# 2. Build the bundle from one or more signed plans:
python -m packaging.build       --crew <name> --out <dir> --allow <dir>/curation-plan.json
```

`plan` writes `curation-plan.json` with every skill and MCP server at
`include:false` and blank `reviewed_by` / `reviewed_at` (`write_plan`,
`_cmd_plan`). To ship an item the operator flips `include:true`, fills in
`reviewed_by` and `reviewed_at`, and passes the file with `--allow`. Guarantees
enforced in `crew/packaging/build.py` `verify`:

- The signature. A plan that selects anything while `reviewed_by` or
  `reviewed_at` is blank is refused. There is deliberately no flag to skip review,
  because a flag fails open when forgotten.
- The content pin. Every reviewed entry records the sha256 of what was reviewed,
  and the build re-checks that hash for each selected entry. A skill or server
  edited after approval refuses the build and is named. Do not hand-edit `sha256`.
- A `blocked` entry (a credential store, a hard credential in a file, or a
  Kiro Crew-managed local MCP server) cannot be included at all.

Running with no `--allow` is a valid outcome: an empty-but-valid bundle carrying
the crew's persona and tools, no private skills, no owner MCP servers
(`merge_plans` returns `None`, `_cmd_build` prints "a valid bundle with the crew's
persona only"). The failure direction is under-sharing. `env` and `headers` on any
selected MCP server are dropped wholesale rather than scanned-and-kept
(`_clean_mcp_server`), so a bespoke token format cannot ride along.

`SMC_BUNDLE_JSON` lists what did NOT ship and why in its `denied` array
(`_denied_list`), so the operator can see the exclusions.

## Diagnose with --why

```
crew/scripts/smc-deploy.sh --why --profile <p> --region <r> --crew <name>
```

This creates and changes nothing (`smc_main` diagnose-only block). It reads the
crew stack's failed resources (`why_failed`) and then the task
(`why_task_died`). It covers two cases, not one:

- A STOPPED task: prints its stop code, exit code, and last log lines. The reason
  a container that started and exited leaves is only in its log stream.
- A RUNNING task, when nothing stopped: prints the live task's last 60 log lines.
  The worst case is a task that is healthy by every infrastructure measure and
  still answers empty; its live log is the only place that says why, so `--why`
  reads it rather than reporting "nothing died."

ECS keeps a stopped task for about an hour, so run `--why` before any redeploy
(a redeploy first deletes a rolled-back stack and the evidence with it).

## Resuming with --from N

`--from N` skips ahead to step N (`smc_main`). Every correctness gate in step 8
runs from persisted or live state regardless of the resume point, so `--from 8`
re-proves the deployment rather than trusting that earlier steps once passed
(`smc_verify` reloads persisted scalars and re-resolves stack outputs).

The resume rule that costs real time: after ANY change to
`crew/templates/base.yaml`, include step 2 (use `--from` no higher than 2). Step
2 is the only step that deploys `base.yaml`, and it deploys it on every run rather
than skipping an existing stack, because skipping let a `base.yaml` change silently
never land (the `# --- 2 . base stack` comment in `crew/scripts/smc-deploy.sh`). The
crew stack consumes the base outputs by exact name (`resolve_base_outputs`,
`deploy/CONTRACT.md` base-outputs table). A base output that never landed resolves
to an empty string, which does not fail at step 2; it fails later as a malformed
parameter value in the crew stack. So `--from 4` after editing `base.yaml`
deploys against infrastructure that does not match the template.

## Traps, each paid for once

These come from the contract files and the driver's own comments. Do not treat
any as hypothetical.

- A fixture that encodes intended rather than real behaviour makes the suite pass
  while the deploy fails. The old `/preflight` gate's fixture answered 200 for a
  route that does not exist, so the gate survived to a real AWS account (`sig()`
  comment in `crew/scripts/smc-deploy.sh`; the rule in `crew/PACKAGING-CONTRACT.md` and
  `crew/runtime/container/CONTRACT.md`: "Fixtures must return what the real thing returns, not
  what the design intends"). When you add or change a fixture, make it match a
  real response.
- The API Gateway stage must be republished after a route is added, which is step
  7. Without it a new crew's route answers 403 as though the crew did not exist,
  and CloudFormation cannot express the republish (`deploy/CONTRACT.md` trap 4;
  step 7 in `crew/scripts/smc-deploy.sh`).
- A secret's ARN carries no version, so a rewritten secret needs a forced new
  deployment. A task reads its secrets once at start, and the crew stack
  references the secret by an unversioned ARN, so rewriting the secret leaves the
  running task on the old value and CloudFormation reports "No changes to deploy."
  Step 5 detects a changed value and step 6 calls
  `force_new_deployment_if_secret_changed` (`crew/scripts/smc-deploy.sh`). Fixing a bad
  key and having nothing happen is the exact failure this prevents.
- The ECS service is `smc-<crew>` while its stack is `smc-crew-<crew>`
  (`CREW_STACK="smc-crew-$CREW"` and `crew_service_name` returning `smc-<crew>` in
  `crew/scripts/smc-deploy.sh`; `deploy/CONTRACT.md` stack-name table). An
  `ecs describe`/`update-service` needs the service name, not the stack name.
- `KIROCREW_BIND` must be `127.0.0.1`. The published base image sets `0.0.0.0`,
  and leaving it puts the backend on the network and removes the design's only
  trust boundary while every local test still passes (`crew/runtime/container/CONTRACT.md`
  env table; gate 5 in `smc_verify`).
- `SMC_CONFIG_DIR` must equal `SMC_DATA_HOME`. Kiro Crew writes `session_map.json`
  and `open_slots.json` at the home root, so a `config/` subdir value backs up
  every transcript and neither resume file, and the backup looks healthy while the
  restore has no resume (`crew/runtime/container/CONTRACT.md`; gate 6 in `smc_verify`).
- Architecture is pinned in two places. Take it from the `CpuArchitecture`
  parameter and cross-check the image's arch against the task's; a hardcoded arch
  or a declared-but-ignored parameter produces an "exec format error" that kills
  the task without saying why (`deploy/CONTRACT.md` trap 1; `judge_arch`).
- On Fargate the model subprocess runs unsandboxed, because Fargate permits no
  user namespace, and that compounds with `--approval yolo`. The decision is
  stated per deploy via `AllowUnsandboxedExec` and printed at step 6; narrowing
  approval to `reads` is the mitigation that does not require leaving Fargate
  (`crew/runtime/container/CONTRACT.md`, the sandbox section).

Several more reported gaps where the contract is silent (a plaintext control
secret assumption, rule-priority uniqueness, a base-parameter name mismatch) are
listed in the PORT NOTES block at the bottom of `crew/scripts/smc-deploy.sh`. They are
reported to other tracks, not fixed in the driver.

## What the 13 verify gates prove

In order, from `smc_verify` in `crew/scripts/smc-deploy.sh`:

1. The service reached steady state, which also proves the ALB's direct `/health`
   check on :8080 is green.
2. Architecture agrees: intended equals the deployed task definition equals the
   image, and `platform_machine` confirms it actually ran (`judge_arch`). An empty
   `platform_machine` is unproven, never agreement.
3. The running task definition serves the exact image `build_crew_image.sh`
   reported, both digest-pinned (`judge_image_digest`). This is the gate whose
   absence let the earlier version report success while serving a default agent:
   with the crew baked in, served image equals reported image equals packaged crew.
4. That image baked in the bundle `packaging.build` produced (`judge_bundle_digest_match`),
   so the served content is the content that was curated.
5. `KIROCREW_BIND` is `127.0.0.1`: the backend is not on the network.
6. `SMC_CONFIG_DIR` equals `SMC_DATA_HOME`: the resume files get backed up.
7. The memory mode holds, and which claim is checked depends on the mode
   (`judge_no_transcripts_restored`). A chatbot crew must report
   `state=disabled` -- nothing is persisted, so no conversation outlives the task.
   A persistent crew must report a clean restore with `transcripts_restored=0`
   AND `transcripts_available > 0`, because the obvious assertion is vacuous on
   its own: a crew with no history would satisfy "restored none" trivially. The
   gate reads one machine-readable boot line and refuses seven ways, including a
   line it cannot find at all -- an absent line means restore never ran, which is
   not the same as restoring nothing.
8. `/health` through the control API answers 200: the owner is SigV4-authorised
   and IAM is the outer boundary (`judge_health_through_control`).
9. A control route with no control secret is refused 403 by the container, not
   merely absent (`judge_control_refused_without_secret`).
10. A forged `X-Control-Secret` is rejected in all three casings, because header
    lookup is case-insensitive and a missed casing would be a real bypass
    (`judge_forged_secret_rejected`).
11. One real turn on the customer path returns a non-empty assistant message with
    a `finish_reason` (`judge_real_turn_response`). A present credential is not a
    working one: an invalid key yields a task that passes its health check and
    fails every turn, so only a real turn establishes the credential works.
12. The prompt fingerprint gate (see below).
13. IAM isolation holds, and this gate FLIPS with the memory mode. In chatbot mode
    the task role must hold no S3 permission at all, its own prefix included -- a
    permission that does not exist cannot be reached by a future code path, which
    is stronger than scoping one. In persistent mode the own `crews/<crew>/` prefix
    is allowed for read and write but never delete, and every other prefix,
    including lookalikes like `crews/<crew>2/` and `crews/<crew>-evil/`, is denied,
    and no bucket policy can widen those denials (`judge_iam`,
    `judge_bucket_policy`).

### Only gate 12 proves whose prompt answered

The prompt-fingerprint gate (`judge_prompt_fingerprint`, fed by
`prompt_fingerprint` and `_inject_fingerprint_challenge` in `crew/packaging/build.py`)
is the only gate that proves the answer came from the packaged prompt. Every other
gate can pass while the wrong crew answers: gates 3 and 4 prove the right artifact
and bundle are deployed, the routing and boundary gates prove the request reached
this crew, and IAM proves isolation, but none of them reaches into the answer.
Gate 10 proves the crew answered something real, yet a stock agent answers "reply
with the single word: ok" indistinguishably from a tuned crew, because that
question has the same answer either way. Gate 11 asks for a value derived from the
bundle's own content, injected into the shipped prompt and present nowhere else,
so a default agent cannot produce it. It is the one gate that depends on the model
complying with an instruction, so its failure message names both causes (the wrong
prompt is serving, or the model declined the instruction), likeliest first.

## Sources

- `crew/PACKAGING-CONTRACT.md` (crew-in-image design, deleted S3 path, deny-by-default) accessed 2026-09-03
- `deploy/CONTRACT.md` (stack and output names, traps) accessed 2026-09-03 -- cited by
  name only: it is NOT in this tree. The driver's own PORT NOTES record it as stale
  and held centrally rather than owned by the deploy track. Every citation of it
  below is to that upstream document.
- `crew/runtime/container/CONTRACT.md` (env contract, sandbox, backup) accessed 2026-09-03
- `crew/scripts/smc-deploy.sh` (steps, gates, `--why`, `--from`, PORT NOTES) accessed 2026-09-03
- `crew/packaging/build.py` (curation, plan, signature, content pin, fingerprint) accessed 2026-09-03
