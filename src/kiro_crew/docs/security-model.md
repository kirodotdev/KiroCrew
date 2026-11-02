# Security model: the layers, and where they stop

Kiro Crew runs an AI agent with real shell and filesystem access on your own
machine. That is the point of it, and it is also the risk: the model reads web
pages, repository files, chat history and imported documents, and any of that
text can try to steer the agent the way a person telling it what to do would.
The two outcomes worth defending against are the agent being talked into
**leaking a credential** and into **destroying something** on the host.

No single control stops that. Kiro Crew stacks several, each covering a
different part of the gap, so that getting past one does not get past the rest.
This page explains the layers, what each one actually protects against, and —
the part most security pages skip — **where they stop**. Read it before you
decide what is safe to run Kiro Crew against: a corporate laptop full of live
credentials is a different trust decision from a throwaway container, and the
honest answer depends on the limits below, not just the protections.

Two ideas run through everything here:

- **The model is untrusted input, not a trusted caller.** Anything the model
  chooses — a command string, a file path, a tool title — could be coming from
  injected text. So the controls key on ground truth (the real command, the
  real resolved path), never on text the model wrote to describe what it is
  doing.
- **You are trusted; the agent is not.** You can widen your own settings. The
  agent must not be able to widen them for you, which is why the files that hold
  your security settings are ones the agent cannot **write** — and the ones that
  carry a live credential it cannot read either (see
  [the trust root](#the-trust-root-the-agent-cannot-rewrite-its-own-limits)).

## The layers

Each layer below is independent. A credential read, to succeed, would have to
get past all of them, and they fail in unrelated ways.

| Layer | What it does | What it protects against |
|---|---|---|
| Interactive approval | Asks you before a tool call runs (opt-in; the default `auto` approves automatically) | A surprising or destructive action you did not intend |
| Denied-command rules | Refuses known-dangerous command shapes at Kiro Crew's own gate | Credential exfiltration, destructive and self-protection commands |
| Filesystem gate | Refuses file-tool reads and writes of credential and settings paths | A tool reading `~/.aws` or rewriting your config |
| Input validation | Checks every Kiro Crew tool call against a schema | Malformed or oversized tool input, hidden-character tricks |
| Output redaction | Strips credentials from everything the agent emits | A secret reaching a human, a chat, or an external service |
| OS sandbox | Confines the agent subprocess with OS-level isolation | A shell reading a credential file the tool gate does not see |
| Audit log | Records security decisions in a tamper-evident log | Not knowing, after the fact, what happened |

Alongside these sit the request authentication for the dashboard, the Slack
owner lock, and — on a managed or enterprise host — a governance ceiling that
your settings cannot loosen. Those are described further down.

One structural point matters more than any single layer: **the denied-command
and governance checks are Kiro Crew's own, not the agent's.** They run in the
gateway, so an agent configuration that omits or edits its own rule list cannot
weaken *them* — a hard deny and an enterprise ceiling hold whatever the agent
config says. This is specifically the deny and governance tier. It is not a
claim that every tool a backend exposes is mediated the same way: the native
tool permissions of an ACP backend (for example the `permissions.deny` rules the
Claude backend seeds from the agent spec) come from configuration the way any
backend's do, so treat Kiro Crew's own gate as the floor, not as a wrapper
around every capability the chosen harness has.

### Interactive approval

Kiro Crew can pause before a tool call and ask you to approve it. Be clear about
the **default**, because it is the opposite of what a security-minded reader
expects: `agent.approval_mode` ships as `auto`, which **auto-approves** tool
calls rather than prompting. Set `agent.approval_mode="interactive"` to make a
tool call that is not already trusted pause and ask you first.

In interactive mode you can approve a call once, trust that exact command, trust
a command pattern, trust read-only commands, or trust everything for the
session. A separate ad-hoc "auto-approve" (YOLO) escalation turns prompting off
for a bounded window and clears on restart. Trust is scoped to the chat that
granted it and is derived from the **actual command**, not the title the model
gave it.

The one thing approval — or its absence — can never do is override a deny. The
deny and governance checks run first, so neither a trust decision nor the `auto`
default can route a blocked action through.

### Denied-command rules

Some command shapes are refused outright at the gate, whether or not you have
approved anything: reading credential files into an outbound request, destructive
infrastructure and data operations, publishing to a protected branch, and the
agent trying to disable Kiro Crew or mint its own dashboard token. You can see
the full list, disable individual rules, disable them all, and add your own
patterns, in **Settings → Security**.

This tier has a real and deliberate limit worth stating once here: it reads the
command line a tool call carries, so a program the line only *names* (a script
file, a runtime-assembled string, an `eval` of a variable) is never opened and a
command hidden inside it is not matched. Read it as defense in depth, not the
control of record — for anything irreversible, that is the layer below (which
credentials the session can reach and what the sandbox lets a process do).
[Blocked commands and credential access](blocked-commands.md) covers why that
limit is deliberate, what each refusal tells the agent to do instead, and how to
check your own credential setup.

### Filesystem gate

Kiro Crew's file tools refuse to read or write a set of sensitive paths:
credential directories (`~/.aws`, `~/.ssh`, `~/.gnupg`, `~/.netrc`, `~/.npmrc`,
`~/.git-credentials`, `~/.config/gcloud`, `~/.azure`, `~/.docker/config.json`,
`~/.kube/config` and their kin), the secret vault, and Kiro Crew's own settings
files. The check runs on the fully symlink-resolved path, so a link pointing
into a blocked directory is refused through the link, and it runs before the
file operation, so every file-read and file-edit tool is covered regardless of
how it opens the file.

The limit to keep in mind: the filesystem gate governs Kiro Crew's **file
tools**. A path read by a raw shell command — a `cat`, a `python -c "open(...)"` —
is **not** matched here; the command gate does not path-match shell text either,
so a shell read of a credential directory is the OS sandbox's job, not this
layer's (see below). That division is exactly why both layers exist.

### The trust root: the agent cannot rewrite its own limits

The files that hold your security settings fall into two dispositions, and the
difference is the honest part. The channel credential `.env` and the secret
vault carry live secrets, so the sandbox **hides** them — the agent can neither
read nor write them. The governance policy and profiles, the denied-command
state, and the runtime config are instead **read-only**: the agent can read them
but cannot write them. They are left readable on purpose — hiding a ceiling file
would make it resolve to the *permissive default*, which is the opposite of what
you want — and read-only is enough, because the risk is an agent rewriting its
own ceiling (setting `disable_all` to switch off the deny gate on the next
restart), not reading it. The result, **wherever the Kiro Crew sandbox is
active**, is that your settings and an enterprise ceiling are **un-disableable
from inside the agent**. That write seal is a Kiro Crew sandbox rule, not an OS
file permission, so the legitimate writers — the dashboard, the channel
handlers, `kirocrew config` from your own terminal — run outside the sandbox and
are unaffected. The flip side is the caveat: the seal is only as present as the
sandbox. Under `agent.sandbox: "off"`, and on a path that runs unconfined
(Windows with no backend), the file is writable by a shell again — so an
enterprise host that depends on this un-disableability pins the sandbox on
through the governance `sandbox.min_level` floor rather than leaving it to
config.

One residual belongs here rather than in the reassuring version: the audit log's
signing key is **not** fenced this way (see
[Where the layers stop](#where-the-layers-stop)).

### Input validation

Every tool call to a Kiro Crew tool is checked against a declared schema before
the handler runs: unicode is normalized and hidden control and formatting
characters are stripped (so a credential cannot be smuggled through an invisible
character), fields are range- and pattern-checked, unknown fields are rejected,
lengths are capped, and tool output is truncated at a ceiling so it cannot become
a denial-of-service vector.

### Output redaction

Wherever agent output reaches a human or leaves the host — the dashboard, a chat
channel, a tool result sent onward — it passes through credential redaction
first. The redactor recognizes credential families in plaintext and in base64
(it decodes base64-looking chunks and re-checks them), flags URLs carrying a
credential or a suspicious payload regardless of destination, and handles the
case where a secret is split across a streaming boundary by holding back the
trailing fragment until it can confirm it is safe. Two rules it follows are worth
stating because they are the usual mistakes: it screens **after** decoding, and
it redacts **before** truncating.

Redaction's boundary is the human and the wire. It does **not** scrub the
model's own context — if the agent reads a secret into its reasoning, redaction
is what stops that secret reaching *you* or an external service, not what keeps
it out of the model.

### OS sandbox

The sandbox confines the agent's subprocess tree with OS-level isolation — a
user and mount namespace on Linux, Seatbelt on macOS — hiding credential
directories and scrubbing credential-bearing environment variables before the
process starts. It is the enforcement point for the case the command gate
deliberately does not cover: a plain shell reading a file.

`agent.sandbox` has three values. The per-tier directory lists — exactly which
paths each tier hides — live in one place,
[Configuration → Sandbox](configuration.md); this page describes what the choice
*means* rather than repeating the table:

- **`auto`** (the default) runs the sandbox at its **standard** tier. Standard
  hides a set of credential and Kiro Crew directories, but it deliberately leaves
  `~/.aws`, `~/.ssh` and `~/.kube` **visible**, because the `aws` CLI, boto3,
  `credential_process`, git-over-SSH and `kubectl` all need them and an agent
  that cannot reach them cannot debug or deploy for you. **Be clear-eyed about
  the consequence: under the default, a shell command such as
  `cat ~/.aws/credentials` succeeds.** The file tools still refuse those paths
  and the credential-shaped commands are still denied, but a direct shell read of
  that directory is not fenced at this tier.
- **`strict`** additionally masks `~/.aws`, `~/.ssh`, `~/.kube` and the other
  credential stores, at the cost of those same tools inside the agent. Set it
  when the host holds credentials the agent must never read and you accept that
  trade.
- **`off`** skips the Kiro Crew sandbox.

The default is a deliberate trade, not an oversight: tightening it for everyone
on an upgrade would silently break the credential tooling most people rely on.
The tier is yours to tighten, and this page names what the default leaves open
so the choice is informed.

Two further properties:

- **A missing sandbox is a refusal, not a silent downgrade.** If a mode other
  than `off` is set and no sandbox backend is available, Kiro Crew refuses to
  spawn rather than running the agent unconfined — unless you opt in explicitly,
  or you are on a platform (Windows) where no backend can ever satisfy the check
  and the default is to allow with the cost stated.
- **Isolation is sometimes delegated to kiro-cli's own sandbox.** On Windows
  there is no Kiro Crew OS sandbox: a positively-identified official Kiro CLI
  spawn delegates to the CLI's own built-in sandbox (with its environment
  scrubbed first), and other subprocesses take the refusal above. On macOS, a
  spawn is delegated the same way when kiro-cli's internal sandbox is enabled,
  because the two seatbelt sandboxes cannot nest. On any delegated path — Windows
  or macOS — Kiro Crew's own credential-directory masks do **not** apply; the
  delegated sandbox's confinement is what holds. The
  [limits](#where-the-layers-stop) section returns to this.

### Audit log

Security decisions — a denial, an auto-approve grant, a sandbox delegation, a
sensitive internal read — are written to an append-only, HMAC-chained Security
Event Log, so tampering between the recorder and an external reader is detectable
rather than merely discouraged. The owner can read recent events and verify the
chain over the API. Several of these writes are fail-closed: an auto-approve
grant, a deny exception and a sandbox delegation refuse to proceed if their audit
record cannot be written.

The honest limit here is twofold. The events are queryable over the API, but
there is **no dashboard to browse, filter or alert on them**, so review and
anomaly-spotting are manual today. And the HMAC chain detects tampering only by
*another* party: the signing key itself is readable and writable by a sandboxed
shell (see [Where the layers stop](#where-the-layers-stop)), so a sufficiently
capable agent could rewrite the log and re-sign it. The chain is evidence against
outside tampering, not proof the agent that produced the events did not alter
them.

## Governance: the enterprise ceiling

On a managed or enterprise host, a second control sits above your own settings: a
governance policy, loaded at startup from the trust root, that your
configuration can only make **tighter**, never looser. It is enforced at the
same gate as everything else, so a policy can deny a tool or command even when
the agent's own configuration granted it, and — because the policy files are
read-only to sandboxed code — the agent cannot rewrite the ceiling it runs under
(it can read it, which is deliberate: a hidden ceiling file would resolve to the
permissive default). On such a host the policy can **pin** denied-command rules
so you cannot disable them, not even with "disable all" — which rules are pinned
is set by the policy's `commands` scope, and composes with your settings on a
tightest-wins basis (the categories an enterprise typically pins are listed in
[Blocked commands](blocked-commands.md)). `kirocrew policy show` and
`kirocrew policy explain` print the ceiling in force.

**Governance reaches computer use through one on/off key only.** Driving native
desktop applications is an opt-in you grant on a protected file, enforced on its
own dispatch path rather than the command gate; it is off by default. That
opt-in is ANDed with the `capabilities.computer_use` policy key: a policy that
turns the key off keeps desktop control off whatever the opt-in says, and an
enterprise build keeps it off until a policy turns the key on. Nothing finer —
which apps, which actions, approval — is governed. See
[Computer use](computer-use.md) for what it can and cannot reach.

## Where the layers stop

Every item here is a real residual. None is a bug to be reported — each is a
trade Kiro Crew made on purpose, named so you can weigh it. These are the facts
that should drive the host-versus-container decision at the end.

- **The default sandbox tier leaves cloud credential directories readable by a
  shell.** As above: under `auto`, `cat ~/.aws/credentials` from a shell
  succeeds. If that is unacceptable for your host, use `strict`.

- **There is no network egress control by default.** The sandbox hides
  credential files; it does not restrict where the agent can send data. A
  compromised agent can post non-credential data to an arbitrary host. Redaction
  blunts the credential case. Where a governance policy is configured, the
  `network.egress` scope bounds the destinations of the agent's dedicated fetch
  tool only; a shell or subprocess (`curl`, a script) is governed only by
  `commands` patterns, which do not bound where the traffic goes. There is no
  default-on network boundary: a network namespace or host firewall allowlist
  would close it.

- **The agent shares the host.** The OS sandbox hides specific directories and
  scrubs specific environment variables; it is not a container or a virtual
  machine. The agent subprocess runs under your user account and shares the
  host's process namespace, so it is isolated *from certain paths*, not sealed
  off from the machine. Anything that runs as your user — including the agent —
  can reach what your user can reach that the sandbox did not specifically hide.

- **The audit log's own signing key is reachable from a shell.** The HMAC key
  that signs the Security Event Log is left readable and writable by a sandboxed
  process in every mode — a knowingly-carried residual, because the in-sandbox
  components that append to the log need the key to do it. So the chain detects
  tampering by an outside party, but an agent capable enough to rewrite the log
  and re-sign it could cover its own tracks. Closing this means moving those
  in-sandbox log writers behind the gateway, not tightening a mask.

- **Your shell startup files are not write-protected.** Credential directories
  and Kiro Crew's own trust root are blocked, but ordinary files like `~/.bashrc`
  or `~/.zshrc` are not — blocking the whole home directory would make the agent
  useless for real work. A write there is a genuine persistence vector, held back
  only by the approval gate and the destructive-command rules.

- **The command gate is not a shell parser.** It normalizes aggressively and
  closes the well-known evasions, but a payload assembled at runtime leaves
  nothing for a pattern to match. See the denied-commands note above; the
  un-disableable guarantees rest on the hidden and read-only trust root, not on
  these patterns.

- **On Windows and on macOS's delegated path, Kiro Crew's own credential masks
  do not apply.** When isolation is delegated to kiro-cli's built-in sandbox,
  Kiro Crew's extra directory masks are not the ones in force; the delegated
  sandbox's own confinement is. On those paths a credential *directory* is
  covered only by the file-tool gate (for the agent's file tools) and by the
  delegated sandbox itself — not by the command gate, which does not path-match
  shell text — and redaction still scrubs any credential that reaches output.

- **Resource ceilings depend on the platform.** Linux uses cgroup limits for
  per-subtree process and memory caps where available; Windows uses Job Object
  limits; macOS and Linux hosts without that support have no hard per-subtree
  ceiling, only a file-descriptor cap and post-failure cleanup.

- **Confinement is not verified from inside the agent.** Kiro Crew decides
  fail-closed whether a backend exists before spawning, but nothing checks from
  within the agent process that the confinement actually took effect. A sandbox
  that loads but does not enforce would not be noticed.

## Choosing a posture for your host

Putting the layers and their limits together:

- **A throwaway container or an isolated personal machine.** The defaults are a
  reasonable fit. The host holds little worth stealing, so the default sandbox
  leaving `~/.aws` shell-readable costs little, and the absence of an egress
  boundary matters less. The `auto` approval default (which approves tool calls
  without asking) is more comfortable here than anywhere else.

- **A corporate or shared machine with live credentials.** Treat the defaults as
  a starting point to tighten, not a finish line. Set `agent.sandbox="strict"`
  so a shell cannot read the credential directories; set
  `agent.approval_mode="interactive"` so tool calls prompt you instead of
  auto-approving; and if outbound data is a concern, put the agent behind a
  network namespace or host firewall, because Kiro Crew ships no egress boundary
  of its own. On a managed host, the governance ceiling is what makes these
  non-optional for the agent — see your administrator.

- **Either way, the agent is not sandboxed from the whole machine.** If a task
  genuinely must not be able to touch the host, run Kiro Crew in a container or a
  VM. The in-product layers raise the cost of the common injection paths; they
  are not a substitute for OS- or hypervisor-level isolation when the blast
  radius has to be zero.

## Related

- [Blocked commands and credential access](blocked-commands.md) — the
  denied-command tier in depth, and how to check your own credential setup.
- [Configuration](configuration.md) — the `agent.sandbox` modes and the full
  config reference.
- [Secrets vault](secrets-vault.md) — storing credentials where the agent cannot
  read them at all.
- [Computer use](computer-use.md) — the capability governance reaches only
  through the single `capabilities.computer_use` key, and what it refuses.
