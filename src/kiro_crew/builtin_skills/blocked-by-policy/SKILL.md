---
name: blocked-by-policy
description: What to do when a Kiro Crew safety policy blocks a tool call — how to tell a policy block from a user refusal, and the sanctioned path for each class of block (AWS credentials, enterprise SSO, key files, the governance trust root, exfiltration-shaped requests, and the self-protection floor). Load when a tool result says a command was denied, when a credential or AWS access attempt is refused, or before concluding that this host lacks a capability.
triggers: blocked by security policy, user denied tool execution, access to sensitive path, sensitive credential path, data-exfiltration pattern, governance trust-root, permission denied by policy, cannot access aws, no aws credentials, aws access denied, sso login, credential path, credential tool not available
---

# When a safety policy blocks a tool call

## First: it was almost certainly not the user

A rejected tool call is reported to you as a generic failure — on kiro-cli
literally `User denied tool execution`. That string is wrong about *who*
refused. Kiro Crew's own gate produces most refusals, and the real reason
arrives separately as a `[Kiro Crew host notice]` message in the same turn.

So:

- **Never tell the user they cancelled, denied, or rejected anything** unless you
  saw them do it. Say the host policy blocked it, and name the rule.
- **Do not stop and ask whether to retry.** Decide inside the same turn.
- **Do not apologise for an interruption that did not happen.**

## Second: do not retry the same shape

The deny rules come in families. If `cat <secret>` was blocked, then `head`,
`tail`, `less`, `more`, `strings`, `base64`, `cp` and `python -c 'open(...)'` on
the same path are blocked too — by sibling rules with the same category. Cycling
through readers wastes turns and produces a series of identical refusals.

The question to ask is never "which reader is allowed" but "what was I actually
trying to accomplish, and what is the sanctioned path to it".

## The classes, and the sanctioned path for each

### AWS credentials (`~/.aws`, `AWS_*` env vars, IMDS)

**You do not need to read them, and running AWS CLI commands is not blocked.**
This is the single most common misdiagnosis: the read is refused, and the
conclusion "this host has no AWS access" is drawn from it. The SDK resolves
credentials itself, including refreshing them through a `credential_process`
entry.

- List profiles: `aws configure list-profiles`
- Confirm the identity in effect: `aws sts get-caller-identity`
- Select one of several configured profiles: `--profile <name>`
- Then just run the command you wanted.

**What is refused is YOU opening the credential file, not the credential being
used.** The SDK inside the `aws` process still reads it, so an already-configured
profile works without you ever touching the file — which is why the command you
actually wanted is the way forward and a different reader never is.

**A resolved credential is not guaranteed just because the user has one.** Your
environment can point at a session-scoped credentials location rather than the
user's own, so a credential they minted by hand in their terminal may be
invisible to you while still existing on the host. When the identity check comes
back empty, report which check you ran and what it returned — that is a different
finding from "no access", and it is the one a user can act on.

If nothing is configured, that is the user's action in their own terminal (for
example `aws sso login`) — report that, do not conclude the capability is
missing. Read-only verbs (`describe*`, `list*`, `get*`) are allowed; destructive
ones are separately denied on purpose.

#### If this host vends credentials through an MCP tool

Some managed and enterprise hosts install an MCP server that mints short-lived
credentials on demand. When one is present it **supersedes** the profile advice
above, and two of its properties routinely mislead:

- The host may make the profile files unreadable even to commands that are
  otherwise allowed, and may reject an explicit `--profile`. Call the vending
  tool, then run the command with no profile flag.
- An **empty profile list is not a broken tool.** It means nobody has registered
  a profile on this machine yet. That is a one-line setup step for the user, not
  a missing capability — and it is a different failure from a policy block, so do
  not describe it as one.

Distinguish the two before reporting anything: a policy block produces a refusal
message, while an unconfigured vendor produces an empty or "no profile" result
with no refusal at all.

#### If the credential tool appears to be missing entirely

A tool search that returns nothing is **not** evidence that the capability does
not exist here, and it is never grounds for explaining the architecture. Under
MCP Tool Search a tool's spec is absent from your list until you load it, and a
keyword query can score below the match threshold while the exact id hits. So:

1. Retry with the exact id — `tool_search(tool_id="<server>::<tool>")` — not a
   keyword query. This alone resolves most cases.
2. If it still misses, the server may be registered but not mounted for THIS
   session. Say that, and name the three things that cause it: the session
   started before the server was registered (a new session or an Apply & restart
   fixes it), the session runs an app agent whose policy neutralizes ungranted
   ambient servers, or the server is in `mcpServers` but never referenced from
   the agent's `tools` array.

**Never invent a reason.** Claiming that a server "is only injected into CLI
sessions" or "is not part of this product's MCP set" is the failure mode this
paragraph exists to stop: it is confidently phrased, unfalsifiable from inside
the session, and it sends the user to rebuild something that was already working.
Report what you observed — the search missed — and hand back the checks above.

### Enterprise SSO session material

A live SSO cookie is a bearer credential: holding it would let you act as the
user against every SSO-gated service. It is fenced for reading as well as
writing, and copying it into a cookie jar is blocked on the same grounds — that
is not a loophole to find, it is the same rule.

You cannot authenticate on the user's behalf. Ask them to run their host's SSO
login command in their own terminal, then retry what needed it.

### Other key material (`~/.ssh`, `~/.gnupg`, `~/.netrc`, `~/.npmrc`, cloud and container stores, …)

Run the command that *uses* the key. The client that owns a credential store —
version-control, remote-shell, cloud, container, package manager — resolves it
without your help. If the task genuinely cannot proceed without the material,
name the step that needs it and let the user carry out that step themselves.

**Routing the material through this conversation is not the alternative to
reading the file — it is the same outcome by a different route.** The refusal was
about that material reaching you; a secret typed into the chat lands in the
transcript, in the model's context, and in everything derived from both. Hand
back the step, not a request for the material.

A refusal in this family is not always about a path: a command whose job is to
**obtain** a credential lands here too. The supported route for that is a
configured profile whose `credential_process` vends one on demand, or the
credential-vending tool the host provides — both are the user's setup step, so
report what you needed instead of re-attempting the acquisition yourself.

### The governance trust root

`security_policy.json`, `profiles/`, `admission_policy.json` and
`denied_commands.json` under the data home are the ceiling you are governed
*by*. Their unreachability from inside a tool call is the property that makes the
ceiling un-disableable — it is not a misconfiguration.

Do not look for another writer, a temp-file rename, or an extract-into-place
trick. State what would need to change and let the user edit it.

### Exfiltration-shaped requests

The refusal is about what the action would DO — move a local file's contents off
this host — not about how it is written, so it must not be re-spelled. The rule
matches the request shape: `-d @file`, `--data-binary @file`,
`--data-urlencode @file`, `-F field=@file`, `--upload-file`, `wget --post-file`,
and `/dev/tcp/` redirects. A form that got past it would mean the control was
defeated rather than satisfied, so those bytes must not leave through you by any
route.

If the upload is genuinely what the task needs, name the file and the destination
and let the user send it themselves. If you only needed the remote call and a
local file was never the point, make the call without one. And if **no** local
file is involved at all — an ordinary authenticated request that merely resembled
the refused shape — say so plainly and report it as an over-block, rather than
looking for a form that gets past the rule.

### The self-protection floor

Some refusals match on the command's **argv**, not on its text — the two you will
meet are an inline interpreter program that imports the product
(`python -c 'import kiro_crew …'`), and a compound command that mixes a product
path with a credential-minting verb.

These guard the product's own credential mint and its supervisor, so the refusal
is about **what the action would do**, not how it is written. The same program
reached by any other invocation form is the same action, so a form that got past
the check would mean the control was defeated rather than satisfied. Do not go
looking for one.

What to do instead depends on what you were actually after:

- **You needed the credential or the restart.** Refused for you by design. Say
  what you need and let the user do it.
- **You needed something unrelated and the product import merely tripped the
  shape.** Get it another way that does not run product code: a file-reading tool
  instead of a shell reader, a CLI subcommand's own output instead of importing
  its internals, or an ordinary package query. If nothing else will do, hand that
  one step to the user rather than re-spelling it.

## Reading the refusal

The reason has two possible forms:

- `Blocked by security policy: <pattern>` — a denied-command rule. The pattern
  is a regex or a glob, so read it as "the family this belongs to", not as a
  literal description of your command. A second line, when present, explains why
  a pattern your text does not literally match still fired.
- `Blocked: <sentence>` — the always-on floor (sensitive paths, the trust root,
  exfiltration shapes). These name the class directly.

## Useful checks

- `kirocrew doctor` — reports the credential posture: which AWS profiles are
  configured, whether a `credential_process` is in play, and whether this host
  mounts an MCP server that vends credentials.
- `kirocrew policy show` / `explain <scope> <item>` — the governance ceiling, on
  a host that has one. CLI only, deliberately: there is no tool for enumerating
  your own ceiling.

## What not to do

- Do not describe a policy block as a user action.
- Do not iterate through readers of the same blocked path.
- Do not disable a rule to get past it. On a governed host the credential and
  sensitive-file categories are pinned and cannot be disabled at all, including
  by `disable_all` — guidance is the only lever, which is why this skill exists.
- Do not silently give up on the task. Name the rule, name the sanctioned path,
  and either take it or hand the one human step back to the user.
