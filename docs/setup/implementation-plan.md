# Implementation Plan — "One command → KiroCrew running on AWS"

> **Status:** Plan to execute. Builds on [`options.md`](options.md) and
> [`research-comparison.md`](research-comparison.md). This is the doc we follow.
>
> **The promise:** the user runs one command, answers *at most* one or two
> questions, and a few minutes later a correctly-sized, correctly-configured
> KiroCrew is running on an EC2 instance **in their own AWS account**, their
> Kiro backend is signed in, and their browser opens straight onto the live
> dashboard. Teardown is one command. KiroCrew stores **no** AWS credentials.

---

## 1. The delight target (the experience we are building toward)

```
$ kirocrew cloud launch            # (the bootstrap installer routes Windows/first-run here)

  [1/6] Prerequisites
        ✓ aws CLI 2.x            ✓ session-manager-plugin
  [2/6] AWS account
        profile 'default' → account 1234-5678-9012 · us-west-2 · reachable ✓
  [3/6] Permissions
        ✓ your identity can create the stack   (else: apply this policy → [open console])
  [4/6] Choose a size
      ◉ Balanced   t4g.xlarge   arm64 · 16 GB · 40 GB disk   ~$0.13/hr   (recommended)
      ○ Light      t4g.large    arm64 ·  8 GB · 30 GB disk   ~$0.07/hr
      ○ Power      m7g.2xlarge  arm64 · 32 GB · 60 GB disk   ~$0.33/hr
  [5/6] Launching  (CloudFormation stack: kirocrew-7f3a)
        ⠸ instance i-0abc booting
        ⠸ installing python · node · kiro-cli · kirocrew        (live setup log)
        ✓ kirocrew.service is up and healthy
  [6/6] Sign in to Kiro
        opened https://…/device?code=WXYZ in your browser — approve to continue…
        ✓ signed in

  🎉  KiroCrew is live on AWS.
      Opening the dashboard → http://localhost:5476/?token=…
      Manage:  kirocrew cloud status | stop | start | destroy
```

Design rules that make this delightful (and non-negotiable):

- **Sensible defaults, minimal questions.** The *only* real choice is the size
  tier (step 4), and even that has a recommended default. Region defaults to the
  profile's region. No AMI IDs, no key pairs, no security-group editing, no
  subnet picking, no SSH-config editing.
- **No secrets to manage.** SSM Session Manager → *no inbound ports, no SSH key
  file*. AWS creds resolved by the `aws` CLI's own chain; we store only a
  profile name (+ region + stack name).
- **Authoritative readiness, not guesswork.** The launch command returns only
  when KiroCrew is actually serving (CloudFormation `WaitCondition` +
  `cfn-signal`), and streams real setup progress while waiting.
- **One-command, atomic teardown.** `kirocrew cloud destroy` → `delete-stack` →
  everything (instance, role, SG, volume) gone. No orphans.
- **Reversible & re-runnable.** Every step is idempotent; re-running `launch`
  finds the existing stack by tag and reconnects instead of duplicating.

---

## 2. Architecture

```
┌─ user's machine (macOS / Windows / Linux) ─────────────┐        ┌─ EC2 (Amazon Linux 2023, arm64) ─────────┐
│  kirocrew cloud …  (Python, ships in the package)      │        │  cloud-init user-data:                    │
│    • aws CLI        (their creds — we store nothing)   │  CFN   │    install python+node+git+kiro-cli       │
│    • session-manager-plugin                            │ deploy │    install kirocrew, kirocrew service     │
│    • run_aws() chokepoint  ── aws cloudformation deploy ──────► │    cfn-signal ✓ when healthy              │
│    • browser / Electron shell                          │        │  kirocrew.service (systemd, loopback)     │
│    • SSM port-forward  ◄── aws ssm start-session ─────────────► │  kiro-cli (backend) — signed in via SSM   │
│      http://localhost:5476?token=…                     │        │  dashboard 127.0.0.1:5476 (never public)  │
└────────────────────────────────────────────────────────┘        └────────────────────────────────────────────┘
                                     no inbound ports · SSM-only · tag-discovered
```

Two halves:

1. **Client** (`kirocrew cloud …`) — a thin, all-OS control plane. Drives the
   `aws` CLI, opens tunnels, opens the browser. This is the part that runs on
   Windows (where the backend can't).
2. **Server** (the EC2 box) — a normal Linux KiroCrew install created by
   cloud-init, supervised by systemd, bound to loopback, reached only via SSM.

---

## 2a. Implementation language — **decision: Python for all logic; thin shell/PowerShell only to bootstrap**

**Question answered:** *"what does our main setup use — `.sh` or Python? Should
we use Python for maintainability?"* → **Today it's a hybrid, and Python is the
right home for everything with logic. We make that split explicit and enforce
it.**

### What "our main setup" is today

It is *already* a hybrid, but an untested one:

- **~1,065 lines of shell** — `install.sh` (571), `setup.sh` (255),
  `ensure-node.sh` (93), `minimal_install.sh` (146) — that bootstrap
  prerequisites (Python, Node, pip, PATH) and detect OS / package managers.
- Then they **delegate to `kirocrew setup`** (Python, `cli_setup.py`, 715 lines)
  for all *configuration* logic (`install.sh` → `kirocrew setup --agent-only`).

The problem is that the **shell layer has almost no unit tests** (its
OS-branching, package-manager detection, and retry logic are untested), while
the Python layer is well covered. The contrast that decides this for the cloud
work: **`deploy_web` has 62 unit tests** precisely because every AWS call routes
through one Python `run_aws()` chokepoint that tests `monkeypatch`. Shell cannot
give us that.

### The rule for this feature

> **All installer/launcher logic lives in Python. The only shell/PowerShell is a
> thin, dumb bootstrapper whose sole job is to make `python3` + `aws` CLI +
> `session-manager-plugin` present, then `exec` the Python wizard.**

| Layer | Language | Size | Why |
|---|---|---|---|
| **Bootstrapper** (`install.sh`, new `install.ps1`, in cloud mode) | shell / PowerShell | ~50–80 lines each, boring & stable | You cannot run Python before Python exists. This is the *only* thing shell is uniquely needed for. Matches openclaw + hermes exactly (*thin bootstrapper → in-CLI wizard*). |
| **Everything else** — wizard, AWS calls, CloudFormation deploy, SSM connect, kiro-cli login, IAM policy gen, lifecycle, cost | **Python** (`src/kiro_crew/cloud/`) | the bulk of the work | One codebase runs on macOS/Windows/Linux; unit-testable via the `run_aws` chokepoint; reuses `deploy_web`, `validation.FieldSpec`, the `Instances` registry, `cli_setup`, `service/`; gated by the repo's `black`/`isort`/`flake8`/`mypy`/`pytest` cycle. |

### Why Python wins for the logic (point-by-point)

| Concern | Shell (`.sh`/`.ps1`) | **Python** |
|---|---|---|
| Unit-testable (mock `aws`, assert exact argv, `AccessDenied→Sid`, idempotent re-launch) | ✗ practically untestable | ✓ `monkeypatch run_aws` — the proven `deploy_web` pattern (62 tests) |
| Cross-platform in **one** codebase | ✗ need `.sh` **and** `.ps1`, kept in sync forever | ✓ one module runs on all three OSes |
| Parse `aws` JSON output, branch, validate input | painful (`jq`, brittle string parsing) | native (`json`, `validation.FieldSpec` charset checks) |
| Reuse existing code | ✗ can't import Python | ✓ `deploy_web`, `Instances`, `cli_setup`, `service/` |
| Repo quality gate (flake8/mypy/black/pytest) | ✗ not linted or type-checked | ✓ same cycle as the rest of `kiro_crew` |
| Error messages, retries, progress rendering | ad-hoc | structured, reusable, testable |

### What this means concretely

- The `src/kiro_crew/cloud/` module map in §12 is **Python**, mirroring
  `deploy_web`'s isolation and its single `run_aws()` chokepoint.
- **`kirocrew cloud launch/status/connect/stop/start/destroy`** are Python
  subcommands (added to the CLI alongside `token`/`service`), so a user with
  KiroCrew already installed never touches a shell script at all — they just run
  `kirocrew cloud launch`.
- The bootstrapper only exists for the **cold-start / Windows** case (no Python
  yet). Once Python + `aws` are present, control is in Python permanently.
- The **user-data on the EC2 box** stays shell (cloud-init runs bash) — but it is
  small and delegates to the existing `install.sh` on the *server*; it is
  smoke-tested in a container (§13), not unit-tested.

### Modularity & testability contract (enforced from M1)

1. **Single AWS chokepoint** — *every* `aws` invocation goes through
   `cloud.aws.run_aws(args, profile, ...)` (sandbox-wrapped, `--profile`, never
   boto3). Nothing else in the module shells out to `aws`. This is the one thing
   tests mock.
2. **Pure functions where possible** — IAM policy generation (`iam.policy_document`),
   size-tier lookup (`sizes`), template parameter assembly, and `AccessDenied→Sid`
   mapping are pure (no I/O), so they unit-test with plain asserts.
3. **I/O at the edges** — subprocess, filesystem, and browser-opening live in
   thin, individually-mockable functions; orchestration (`wizard`, `ec2.deploy`)
   composes them.
4. **Validate before argv** — every user/LLM-influenceable value (`profile`,
   `region`, `instance_type`, stack name) passes a `validation.FieldSpec` before
   it reaches a subprocess (exactly as `deploy_web` validates `profile`/`region`).
5. **No magic values** — instance types, prices, timeouts, tag keys, the SSM
   document name live in `cloud/sizes.py` / module constants, per the repo's
   "no hardcoded values in business logic" rule.
6. **Every verb has a `--dry-run`** that returns the exact argv without executing
   — both a UX feature and the primary assertion surface for tests.

This gives us the same 60+-test confidence `deploy_web` has, from M1 onward.

---

## 3. Provisioning approach — **decision: CloudFormation**

We evaluated three ways to create the stack of resources (IAM role + instance
profile, security group, EC2 instance, EBS volume, tags):

| Approach | Verdict |
|---|---|
| **Raw `aws ec2 run-instances` (imperative, like `deploy_web`)** | ❌ Works for one resource; a *multi-resource stack* means hand-rolled ordering, no atomic rollback, brittle idempotency, and messy teardown (we'd re-implement `deploy_web`'s partial-recovery logic ×N). |
| **CDK** | ❌ Requires Node **and `cdk bootstrap`** (which itself creates an S3 bucket + roles + needs broad IAM in the user's account). Too much friction/permission for an end-user installer. `cdk deploy` is CloudFormation underneath anyway. |
| **CloudFormation** | ✅ **Chosen.** One declarative template, one `aws cloudformation deploy`, **atomic create/rollback**, **one-command `delete-stack` teardown**, tag-based discovery for free, drift detection. Ships as a static YAML in the package. **Zero extra runtime** — the `aws` CLI already has `cloudformation deploy/describe-stacks/delete-stack`, so it preserves the "shell to `aws` CLI, never boto3" boundary exactly. |

**Authoring note:** if we ever prefer CDK ergonomics for *writing* the template,
we can `cdk synth` it to a static CloudFormation YAML we ship — the end user
still only ever runs plain `aws cloudformation deploy`, no CDK toolchain on their
machine. For v1 a hand-written YAML is simplest and has no toolchain at all.

**Two CloudFormation tricks that carry most of the delight:**

- **`resolve:ssm` AMI aliases** — never hardcode per-region AMI IDs. The template
  uses
  `'{{resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64}}'`
  so it always picks the latest Amazon Linux 2023 for the chosen region/arch,
  automatically.
- **`WaitCondition` + `cfn-signal`** — user-data calls `cfn-signal` only after
  `kirocrew.service` is healthy, so `aws cloudformation deploy` blocks until the
  box is *actually ready*. If setup fails, the stack rolls back to a clean state
  (no half-provisioned orphan).

---

## 4. The CloudFormation template (`src/kiro_crew/cloud/templates/kirocrew-ec2.yaml`)

**Parameters** (all defaulted; the wizard overrides via `--parameter-overrides`):

| Parameter | Default | Notes |
|---|---|---|
| `InstanceType` | `t4g.xlarge` | from the size tier (§5) |
| `Architecture` | `arm64` | drives the AMI alias; `x86_64` alternative |
| `VolumeSizeGb` | `40` | gp3 root volume |
| `VpcId` / `SubnetId` | *(auto-discovered default VPC)* | wizard resolves and passes them |
| `StackNameTag` | `kirocrew-<rand>` | for tag discovery |
| `AllowSshCidr` | `""` (empty = SSM-only) | SSH fallback only if set |

**Resources:**

- **`IAM Role` + `InstanceProfile`** — trust `ec2.amazonaws.com`; attach the
  AWS-managed `AmazonSSMManagedInstanceCore` (enables Session Manager with no
  inbound ports). Optionally CloudWatch agent policy for logs later.
- **`SecurityGroup`** — **no inbound rules** by default (SSM is outbound-only);
  egress all. If `AllowSshCidr` is set, add a single 22/tcp ingress from that
  CIDR (the fallback path).
- **`EC2 Instance`** — `ImageId` via the `resolve:ssm` alias, `IamInstanceProfile`,
  the SG, a gp3 `BlockDeviceMapping`, `UserData` (§6), and tags
  `kirocrew:managed=true` + `kirocrew:instance=<StackNameTag>`. Public IP for
  outbound only (package/SSM egress).
- **`WaitCondition` + `WaitConditionHandle`** — user-data signals success/failure.

**Outputs:** `InstanceId`, `Region`, `PublicDnsName` (diagnostics only — we never
connect to it directly), `StackName`.

**VPC choice (open question, see §12):** v1 uses the account's **default VPC +
a public subnet** (auto-discovered by the wizard, passed as params) — simplest,
most delightful. Fallback for accounts with no default VPC: a later template
variant that creates a minimal VPC (VPC + public subnet + IGW + route). Flagged
as a decision point, not built in v1.

---

## 5. "Appropriately sized" — the size tiers

`REMOTE_DESKTOP_SETUP.md` states KiroCrew itself uses ~10 GB RAM, with spikes
beyond that (16 GB comfortable). So the **default must be ≥16 GB RAM**. We default
to **arm64 / Graviton** — cheaper per GB, and both KiroCrew and `kiro-cli` ship
aarch64 Linux builds.

| Tier | Instance | Arch | RAM | vCPU | Disk | ~On-demand |
|---|---|---|---|---|---|---|
| Light | `t4g.large` | arm64 | 8 GB | 2 | 30 GB | ~$0.07/hr |
| **Balanced (default)** | `t4g.xlarge` | arm64 | 16 GB | 4 | 40 GB | ~$0.13/hr |
| Power | `m7g.2xlarge` | arm64 | 32 GB | 8 | 60 GB | ~$0.33/hr |

- Prices are illustrative (region-dependent) — the wizard shows a **live**
  estimate via `aws pricing`/a small static table and always labels it "approx."
- An **x86_64 lane** (`t3.xlarge` / `m7i.2xlarge`) is offered for users who need
  it; it just flips the `Architecture` param + AMI alias.
- `t4g` burstable is deliberately chosen for the default: cheap at idle, bursts
  for tool calls. Power tier uses non-burstable `m7g` for sustained load.

---

## 6. What runs on the box (cloud-init user-data)

User-data is a bash script embedded in the template (kept small; it downloads the
real bootstrap from the repo so we're not maintaining a huge inline script):

1. Detect distro/arch; install `python3`, `python3-venv`, `git`, Node 20+
   (`dnf`/`apt`), `ripgrep`, `ffmpeg` (mirror `install.sh`'s dependency logic).
2. Install **`kiro-cli`** (the AL2023 arm64 path from
   `docs/kiro-cli/installation.md`).
3. Install **KiroCrew** — v1: `git clone` + `bash install.sh` (today's path).
   (Later: `pip install kirocrew` once the wheel is published — Decision 7 in
   `options.md`.)
4. `kirocrew setup --agent-only` + **`kirocrew service install`** (systemd,
   loopback bind, auto-restart, boot-survival — already documented in
   `REMOTE_DESKTOP_SETUP.md`).
5. Health-check the gateway locally (`curl 127.0.0.1:5476` / `kirocrew doctor`),
   then **`cfn-signal`** success (or failure with the tail of the setup log).
6. Setup log streamed to `/var/log/kirocrew-setup.log` — the client tails it over
   SSM for the live progress in step 5 of the UX.

Note: `kiro-cli` is **not** logged in yet at this point — that's an interactive
human step handled next (§7).

---

## 7. Backend (Kiro) sign-in on the box

Can't be baked into user-data (needs a human to approve in a browser, possibly to
purchase a subscription). Done as a post-provision wizard step over SSM:

- Run `kiro-cli login` on the instance via `aws ssm start-session` (or
  `send-command`), **scrape the device-code URL + code** from its output.
- **Builder ID / IAM Identity Center → device code:** auto-open the URL in the
  user's *local* browser; poll `kiro-cli`/a marker until logged in. No port
  forward needed.
- **Social (Google/GitHub) → SSH `-L`-style forward:** kiro-cli's remote flow
  needs a forwarded port; we set up the equivalent SSM port-forward automatically
  and open the URL locally. (`docs/kiro-cli/authentication.md` documents both.)
- "Register / purchase a Kiro subscription" is simply a link into that same
  kiro.dev / kiro-cli flow — we **guide, we don't reimplement** billing.
- **We store no Kiro credentials** — they live in kiro-cli's own store on the
  instance.

---

## 8. Connect — web-UI access from browser or app

Once ready + signed in:

1. Mint a dashboard token on the box: `kirocrew token` over SSM (the CLI already
   prints `http://localhost:5476/?token=…`).
2. Open an **SSM port-forward**:
   `aws ssm start-session --target <id> --document-name AWS-StartPortForwardingSession --parameters portNumber=5476,localPortNumber=5476`.
3. Open `http://localhost:5476/?token=…` in the browser (or the Electron shell).

- The gateway stays **loopback-only** on EC2 — never `0.0.0.0` — matching the
  current security posture; access is always tunnel + token.
- **Instances integration (managed experience):** register the box in
  `~/.kirocrew/instances.json` (the existing `Instances` registry). Its
  `ssh_host` can be an SSM `ProxyCommand` alias — **already documented in
  `docs/INSTANCES.md` §9**. Then the hub dashboard's `/instances` page gives
  auto-tunnel + token-refresh + self-heal + iframe switching for free. The
  `Instances` feature currently drives `ssh`; we either (a) register an
  SSM-`ProxyCommand` ssh alias (zero code change, works today) or (b) add an
  SSM transport to `ssh_tunnel_manager` (cleaner, later).
- **Desktop app:** the Electron shell wraps the same client — "open dashboard"
  button = the port-forward + token-open above. This is the all-OS access path.

---

## 9. Client-side prerequisites & bootstrap (all OSes, incl. Windows)

The client needs only: **`aws` CLI v2**, the **`session-manager-plugin`**, and a
browser. All three exist for macOS/Windows/Linux.

- Extend `install.sh` and add **`install.ps1`** (Windows) whose *only* job in
  cloud mode is to ensure `aws` + `session-manager-plugin` (+ Python to run the
  wizard). Reuse the peers' Windows tricks (emulation-invariant arch detection;
  portable bootstrap; `run_with_timeout` stall-killer; reactive retry).
- `session-manager-plugin` is a small per-OS installer (pkg/msi/rpm/deb) —
  bootstrap it, verify with `session-manager-plugin --version`.
- **SSH fallback** (if the user opts out of SSM): OpenSSH is built into Win10+ /
  macOS / Linux, so no plugin — but then we manage a key pair + a scoped SG
  ingress. SSM stays the recommended default precisely to avoid that.

---

## 10. IAM — least-privilege policy we generate for the user

Same philosophy as `deploy_web/iam.py`: **KiroCrew never performs an IAM write.**
We *generate* the policy text; the user applies it (or, if they're the account
admin, they already have it). Reachability is read-only (`sts get-caller-identity`,
`ec2 describe-*`); the first `cloudformation deploy` is the true permission test,
and on `AccessDenied` we map stderr → the exact missing action/statement.

The launch path needs (scoped by the `kirocrew:managed` tag / stack name where
the API supports it):

- `cloudformation:CreateStack/UpdateStack/DeleteStack/DescribeStacks/DescribeStackEvents/GetTemplateSummary`
- `ec2:RunInstances/DescribeInstances/DescribeImages/DescribeVpcs/DescribeSubnets/DescribeSecurityGroups/CreateSecurityGroup/AuthorizeSecurityGroupEgress/CreateTags`, and tag-scoped `StopInstances/StartInstances/TerminateInstances`
- `iam:CreateRole/DeleteRole/CreateInstanceProfile/DeleteInstanceProfile/AddRoleToInstanceProfile/RemoveRoleFromInstanceProfile/AttachRolePolicy/DetachRolePolicy/PassRole` (PassRole scoped to the created role) — this needs **`CAPABILITY_IAM`** on the deploy, which we surface explicitly
- `ssm:StartSession/TerminateSession/DescribeInstanceInformation/SendCommand/GetCommandInvocation` and `ssm:StartSession` on the `AWS-StartPortForwardingSession` document
- `ssm:GetParameters` on the public AMI alias path

This IAM surface is **larger than `deploy_web`'s** (it creates a role + passes it)
— we call that out honestly in the wizard and recommend the SSO/IdC admin-ish
profile the "bring-your-own-AWS account owner" audience already has.

---

## 11. Lifecycle, cost & safety

**Command surface — `kirocrew cloud <verb>`** (human entry point; the wizard
calls into it):

| Verb | Action |
|---|---|
| `launch` | the full wizard (§1); idempotent — reconnects if the tagged stack exists |
| `status` | discover by tag → instance state, cost-so-far estimate, health |
| `connect` | (re)open the SSM port-forward + token, open browser |
| `stop` / `start` | `ec2 stop/start-instances` (pause billing for compute; EBS still bills) |
| `destroy` | `cloudformation delete-stack` (confirm-first) → all resources gone |

**Safety (preserve the existing asymmetry):**

- `aws ec2 terminate-instances` and `aws ec2 delete-*` are already in the
  **agent** deny-list, and `~/.aws`/`~/.ssh` are sensitive-path-blocked. Cloud
  provisioning is a **human/installer** action, **never an LLM tool**. Keep it
  that way: `launch`/`stop`/`start`/`destroy` are human CLI/UI verbs; at most a
  read-only `cloud_status` could later become an MCP tool.
- `destroy` requires explicit confirmation and names exactly what it will delete
  (matches the global production-safety rules).
- **Cost delight:** show a live "$X.XX so far / ~$Y/day if left on" line in
  `status`; offer an **optional idle-stop** (a lightweight self-stop timer on the
  box, or a scheduled stop) — hermes's scale-to-zero is the reference. Nice-to-have,
  not v1.

---

## 12. Codebase layout & reuse

New module, mirroring `deploy_web`'s isolation and its `run_aws` chokepoint so
tests can monkeypatch one function:

```
src/kiro_crew/cloud/
├── __init__.py
├── aws.py             # run_aws() chokepoint (sandbox-wrapped, --profile, never boto3)
├── ec2.py             # deploy/status/stop/start/destroy via `aws cloudformation` + `aws ec2`
├── iam.py             # policy_document() generator + reachability_check()  (mirror deploy_web/iam.py)
├── connect.py         # SSM port-forward + token mint + open browser; Instances registration
├── login.py           # kiro-cli sign-in over SSM (device-code scrape / port-forward)
├── sizes.py           # the size-tier table (§5) + price hints  (constants, no magic numbers)
├── wizard.py          # the 6-step interactive flow (§1)
└── templates/
    └── kirocrew-ec2.yaml
```

Reuse, don't reinvent:

- **`deploy_web`** — copy the `run_aws` chokepoint, `AccessDenied→Sid` mapping,
  tag-based statelessness, IAM-policy-for-the-user, reachability checks.
- **`validation.py` `FieldSpec`** — charset-validate every LLM/user-influenceable
  value (`profile`, `region`, `instance_type`, stack name) before it hits argv
  (exactly as `deploy_web` validates `profile`/`region`).
- **`Instances` registry + `ssh_tunnel_manager`** — reuse for the managed connect
  experience; SSM `ProxyCommand` alias works today with zero changes.
- **`cli_setup.py` / `cli_server.py`** — the **thin integration layer**, not where
  logic lives. `cli_setup.py` already owns the interactive wizard
  (`_setup_workspace_dir`, `_setup_slack_tokens`, `_maybe_setup_dashboard_url`,
  …); we **add one `_maybe_setup_cloud()` step** that *calls into* `cloud.wizard`
  — the same delegation pattern the file already uses for Electron/Slack/timezone.
  `cli_server.py` gets the `cloud` subcommand group (`launch/status/connect/…`)
  next to `token`/`service`, each a **1–3 line dispatcher** into `cloud.*`. All
  real logic stays in the testable `cloud/` module; `cli_*` stays a wiring layer.
  (See the note below on why we *don't* pile logic into `cli_setup.py`.)
- **`service/`** — the box uses the existing `kirocrew service install`.

> **On "should we just improve `cli_setup.py`?"** — Yes to *extending the wizard*
> there (one new step that offers the cloud path), **no to putting cloud logic
> in it.** `cli_setup.py` is already 715 lines and is a UX/prompt orchestrator,
> not a unit-tested logic module. Piling AWS/CloudFormation/SSM logic into it
> would repeat the *exact* mistake the shell installers made — untestable logic
> tangled with I/O. Instead, `cloud/` is the testable engine (the `deploy_web`
> shape), and `cli_setup.py`/`cli_server.py` are the thin callers. This keeps
> `cli_setup` maintainable *and* gives the cloud code 60+ tests.

---

## 13. Testing strategy

- **Unit:** monkeypatch `cloud.aws.run_aws` (single chokepoint, like `deploy_web`'s
  54 tests) — assert exact argv for each verb, `AccessDenied→Sid` mapping,
  idempotent re-launch (tag found → reconnect), and validation rejects bad
  profile/region/instance-type.
- **Template:** validate `kirocrew-ec2.yaml` with `aws cloudformation
  validate-template` in CI; a `cfn-lint` pass; snapshot the rendered parameters.
- **User-data:** lint the bootstrap script (shellcheck); a container-based smoke
  test that runs it against a Linux image to catch install regressions
  (no real EC2 in CI).
- **Live (manual / gated):** a `--dry-run` on `launch` that prints the exact
  `aws cloudformation deploy` argv without executing; a gated end-to-end test in
  a scratch AWS account behind an env flag.
- Follow the repo rules: `@pytest.mark.asyncio` on async tests, mock all external
  processes, never spawn real `aws`/`kiro-cli` in CI.

---

## 14. Milestones (follow in order; each is independently shippable)

**M1 — Client bootstrap + read-only `cloud` skeleton.**
`cloud/aws.py` chokepoint, `cloud/iam.py` (policy generator + reachability),
`kirocrew cloud status` (tag discovery, empty-state), `install.ps1` cloud-mode
prereqs (`aws` + `session-manager-plugin`). *Acceptance:* on a machine with a
configured profile, `kirocrew cloud status` reports "no instances" and the IAM
policy + reachability check render correctly; unit tests green.

**M2 — Template + `launch` (provision only).**
`kirocrew-ec2.yaml` (role/SG/instance/waitcondition, `resolve:ssm` AMI),
`cloud/ec2.py deploy()`, size tiers, `--dry-run`. User-data installs KiroCrew +
service and `cfn-signal`s. *Acceptance:* `launch --dry-run` prints correct argv;
a real `launch` in a scratch account brings up a box where `kirocrew.service` is
healthy and the stack reaches `CREATE_COMPLETE`; `destroy` cleanly deletes it.

**M3 — Backend sign-in over SSM.**
`cloud/login.py` — scrape device-code URL, auto-open locally, poll to done.
*Acceptance:* after `launch`, the wizard signs kiro-cli in on the box with one
browser approval.

**M4 — Connect + one-click dashboard.**
`cloud/connect.py` — SSM port-forward + token + open browser; register as an
Instance. *Acceptance:* browser opens on the live remote dashboard; the
`/instances` page can switch to it.

**M5 — Lifecycle, cost & polish.**
`stop`/`start`/`destroy` with confirmation + cost estimate in `status`;
`doctor --json`/`--fix`; end-of-setup "start + verify reachable" gate.
*Acceptance:* full `launch → use → stop → start → destroy` loop is clean and
idempotent; agent deny-list verified intact.

**M6 — Distribution (unblocks faster boxes).**
Publish the pip wheel → user-data switches from `git clone` to `pip install
kirocrew`; optional Docker image. (Decision 7 in `options.md`.)

---

## 15. Risks & open questions (confirm before/at M2)

1. **VPC:** default VPC + public subnet (v1, simplest) vs. stack-created VPC
   (robust for accounts with no default VPC). Recommend v1 = default VPC, add
   create-VPC variant later.
2. **IAM ask is non-trivial** (create role + PassRole + `CAPABILITY_IAM`).
   Acceptable for the "account owner" audience (same bar as `deploy_web`)?
   Confirm we surface it honestly and don't try to shrink below what CFN needs.
3. **`session-manager-plugin` as a client dependency** — accept the extra install
   for the "no open ports, no key file" delight? (Recommended yes; SSH is the
   fallback.)
4. **Default region/size** — ship `t4g.xlarge` arm64 as the recommended default,
   region from the profile? Or always ask?
5. **Idle-stop / scale-to-zero** — in scope for v1 or a fast-follow?
6. **`git clone + install.sh` vs. published wheel** in user-data for v1 — start
   with clone (no publishing dependency), migrate at M6?
