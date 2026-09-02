# Kiro Crew behind enterprise MCP governance

Applies when the Kiro account `kiro-cli` is signed in to is an **enterprise**
account — IAM Identity Center (or an external IdP such as Okta / Entra ID
fronting it), or an API key — and an administrator has configured an **MCP
registry**. Personal accounts (Builder ID, social sign-in) are not subject to
organization-level MCP controls and need nothing on this page.

[Central policy distribution](#central-policy-distribution-one-security-policy-every-host),
at the end of this page, is a separate axis and depends on none of that: it is how
you hand every host in a fleet the same Kiro Crew `security_policy.json`, and it
works on any account type.

## The symptom

Kiro Crew starts, the dashboard works, chat works — and a large part of the
product is quietly absent. `spawn_run` does nothing, `cron_add` is unavailable,
`learn_add` never saves, the knowledge tools are missing, the research agent has
no tools to work with. Nothing errors. `kirocrew doctor` reports the MCP servers
healthy.

That combination — healthy locally, absent in sessions — is the signature of MCP
governance, because the two checks are measuring different things:

- Kiro Crew's own probe **spawns each server directly** and completes an MCP
  handshake with it. That succeeds regardless of governance.
- `kiro-cli` applies governance **when it assembles a session**, after reading
  the agent spec. Every server it drops there is dropped silently.

## What governance actually does

The administrator sets two things on the Kiro profile (Kiro console → Settings →
Shared settings): an MCP on/off toggle, and an **MCP Registry URL** pointing at a
registry JSON file listing the allow-listed servers.

With a registry URL configured, the client is in **registry access mode**, and
its filter is *symmetric*:

| Access mode | Entries that connect | Entries that are dropped |
|---|---|---|
| registry (a registry URL is set) | only entries carrying `"type": "registry"` that resolve to a catalog entry **of the same name** | everything else |
| non-registry (no registry URL) | ordinary entries | entries carrying `"type": "registry"` |

Two consequences worth internalising:

- The match is on the **`mcpServers` map key**, not on the command, not on a
  registry id. `kirocrew-core` in your spec must be `kirocrew-core` in the
  registry file.
- `"type": "registry"` is **not a transport**. It declares "this entry is a
  pointer into the catalog", and only `env`, `headers` and `timeout` are carried
  over from your entry as overrides. The `command` in a registry-type entry is
  not what launches.

Governance also **fails closed**: if the client cannot reach the governance API,
MCP is disabled entirely rather than falling open.

## Fixing it — two halves, both required

### 1. Declare registry mode on the Kiro Crew side

```bash
kirocrew config set agent.mcp_registry_mode true
kirocrew restart
```

Kiro Crew then stamps `"type": "registry"` on the servers it manages, so they
survive the registry filter. It is an explicit declaration rather than
auto-detection on purpose: the client fetches the toggle and the registry URL
from `GetProfile` at startup and **persists neither**, so nothing on disk
distinguishes a governed account from an ungoverned one. Leave the setting
`false` on a personal account — there the filter inverts and the marked entries
are the ones dropped.

Verify with `kirocrew doctor`, which grows an `MCP Governance (enterprise)`
section whenever the local identity came from Identity Center.

### 2. Have the administrator allow-list the servers

Kiro Crew needs three servers, and they must appear in the registry file under
**exactly** these names:

| Server | What is lost without it |
|---|---|
| `kirocrew-core` | `spawn_run`, `learn_add`, artifacts, knowledge, monitoring — the bulk of the product |
| `kirocrew-cron` | every scheduled job (`cron_add` and the whole cron surface) |
| `kirocrew-computer` | desktop automation (inert unless separately enabled, but still filtered) |

The registry file format is a subset of the MCP registry standard's server
schema. Each entry needs a `packages` entry describing how to launch the server,
and — because all three Kiro Crew servers live behind one package — a
`packageArguments` entry naming the subcommand. For a `pypi` package the client
derives `uvx <identifier> <packageArguments>`, so an entry without the argument
launches `uvx kirocrew` with no subcommand, which prints CLI help instead of
speaking MCP and fails the handshake:

```json
{
  "servers": [
    {
      "name": "kirocrew-core",
      "description": "Kiro Crew orchestration: subagents, memory, artifacts, monitoring",
      "version": "0.3.0",
      "packages": [
        {
          "registryType": "pypi",
          "identifier": "kirocrew",
          "packageArguments": [{ "type": "positional", "value": "mcp-core" }],
          "transport": { "type": "stdio" }
        }
      ]
    },
    {
      "name": "kirocrew-cron",
      "description": "Kiro Crew scheduled jobs",
      "version": "0.3.0",
      "packages": [
        {
          "registryType": "pypi",
          "identifier": "kirocrew",
          "packageArguments": [{ "type": "positional", "value": "mcp-cron" }],
          "transport": { "type": "stdio" }
        }
      ]
    },
    {
      "name": "kirocrew-computer",
      "description": "Kiro Crew desktop automation (macOS, opt-in)",
      "version": "0.3.0",
      "packages": [
        {
          "registryType": "pypi",
          "identifier": "kirocrew",
          "packageArguments": [{ "type": "positional", "value": "mcp-computer" }],
          "transport": { "type": "stdio" }
        }
      ]
    }
  ]
}
```

Set `version` to the Kiro Crew version your fleet runs.

## Known limitation: the registry launches the server, not your install

Kiro Crew's MCP servers are not standalone tools — they are the gateway's own
process, reached through subcommands (`kirocrew mcp-core`, `mcp-cron`,
`mcp-computer`), and they share the gateway's data home and version.

A registry-type entry hands the launch decision to the catalog: the client
resolves the package and, when a locally installed server's version differs from
the registry's, relaunches it at the registry's version. For a `pypi` entry that
means `uvx` fetching Kiro Crew from PyPI into its own ephemeral environment — so
the process serving your MCP tools can be a *different* Kiro Crew from the
gateway serving your dashboard. Your `env` overrides (including `KIROCREW_HOME`)
do flow through, which keeps the data home aligned, but the code does not.

Keep the registry `version` in step with your fleet's installed version. If your
organisation pins Kiro Crew centrally, that pin now governs the MCP side too.

## Version floor

MCP registry governance requires Kiro CLI **1.23** or later (Kiro IDE 0.11.28).
Enforcement in the V2 TUI arrived in **2.2.2**, and **2.6.0** made personal
`mcp.json` servers load alongside registry-managed ones. Kiro Crew's servers
live in an agent spec (`~/.kiro/agents/kirocrew.json`), not in personal
`mcp.json`, so that last change does not exempt them.

## Central policy distribution: one security policy, every host

`security_policy.json` is Kiro Crew's **enterprise ceiling**: the trust-root
document that denies tools, commands, filesystem paths and MCP calls at Kiro
Crew's own gate, and that the agent can neither read nor rewrite. It is a
different mechanism from the MCP registry above — it governs Kiro Crew rather
than what `kiro-cli` will connect to — and it needs no enterprise Kiro account.

By default it is a local file on each host, which makes every change to it a
config-management job. Point a host at a **central source** instead and it
fetches the document at boot, keeps the last copy that worked on disk, and
re-fetches on an interval, so a change you publish once binds on every host with
no restart, no redeploy and no visit to the host.

Two properties to internalise before you design a rollout:

- **It is a pull on an interval, not a push.** There is no channel that reaches a
  host on demand, so a change lands *within* one refresh interval rather than
  instantly, and a host that is off takes it when it next starts. The poller waits
  a full interval before its first run, since boot has just fetched from the same
  source — so a fleet restarting together does not stampede your endpoint.
- **The document is the whole ceiling, not a patch.** The fetched policy replaces
  the local one outright; there is no merge with `~/.kiro/crew/security_policy.json`
  and no per-host addendum. Anything a host needs must be in the published
  document (or in a narrower per-surface profile, which can only tighten).

### The two ways to point a host at a source

| | `KIROCREW_POLICY_URL` | a `distribution` block in a policy file |
|---|---|---|
| Where it lives | per-machine environment | inside a `security_policy.json` some host already has |
| Set by | Jamf / Intune / Ansible / Chef / Puppet, a systemd unit, a container env | placing one small bootstrap policy once |
| Carries a credential | yes, via `KIROCREW_POLICY_HEADERS` | never — see below |
| Reach for it when | your config-management tool already reaches every host and you want no file to place | you would rather place one file once and have the published document name where its own successors come from |

The two compose, and the environment wins **per setting** — so a host can be
redirected to a canary endpoint, or have its interval lengthened during an
incident, without editing (and re-signing) the document the rest of the fleet is
reading.

```bash
KIROCREW_POLICY_URL=https://config.corp.example/kirocrew/security_policy.json
KIROCREW_POLICY_HEADERS='{"Authorization":"Bearer <per-machine token>"}'
KIROCREW_POLICY_REFRESH_SECS=900          # 0 = fetch at boot only; floor is 60
KIROCREW_POLICY_TIMEOUT_SECS=10           # per request; default 10
KIROCREW_POLICY_MAX_CACHE_AGE_SECS=86400  # 0 = no staleness bound
KIROCREW_POLICY_ON_UNAVAILABLE=fail_closed  # or: degrade
```

The `distribution` block spells the same settings as policy keys — `source`,
`refresh_interval_secs`, `timeout_secs`, `max_cache_age_secs`, `on_unavailable` — and
[`assets/security-policy.example.json`](assets/security-policy.example.json)
shows one filled in alongside the rest of a policy. Read the `network.egress` and
`commands` rows in that example as **egress defense-in-depth, not a bounded
egress guarantee**: a deny list is a finite set of known patterns and cannot
enumerate every network-capable tool. Neither row constrains the policy fetch
itself — that is Kiro Crew reaching for its own ceiling before any agent exists,
not a governed tool call, so your source does not need to appear in an egress
allow-list.

**If you use the block channel, put the block in the published document too.**
Once a host is up it reads its refresh settings from the ceiling *currently in
effect* — which, after the first fetch, is the document you published. A bootstrap
policy names the source for the boot fetch; a published document that omits
`distribution` leaves the host with no source to poll, so it fetches once at
startup and then never again. Carrying the block forward in every published
revision is what makes the channel self-sustaining. The environment channel has no
such trap: `KIROCREW_POLICY_URL` is read from the environment on every poll. Either
way, `refresh_interval_secs` (or `KIROCREW_POLICY_REFRESH_SECS`) must be non-zero
for a background poller to exist at all — `0` means fetch at boot only, which is
still centrally managed but not "on the fly".

**Do not put a request credential in the published document.** The block has no
`headers` field on purpose: that document goes to the whole fleet, is copied into
a local cache on every host, and is reported on by a read-only viewer. Per-machine
credentials belong in `KIROCREW_POLICY_HEADERS` (a JSON object of header name to
value).

**Transports.** `https` works anywhere. `file://` must name a **local path** — an
NFS/SMB share is fine, but mount it and name the mount point, because a
`file://server/share/...` URL is refused. It must also be **read-only to the account
Kiro Crew runs as — the file *and* every directory above it**: a source that account can write is one an agent subprocess can
write, and the refresher would install that ceiling without a restart. A `0444` file in a
writable directory does not count: it can be replaced by unlink-and-recreate. Use a
root-owned path or a read-only mount; if what you want is a local, editable policy file, that is
`KIROCREW_SECURITY_POLICY` (tier 1), not this channel. The validator is a content digest,
so a host on a shared mount re-reads only when the bytes actually change — including the
case where you replace the file with a same-size version and preserve its timestamp.
Plain
`http` is accepted only for a loopback host, because a clear-text ceiling can be
substituted in transit by anyone on the path. A scheme with no transport behind it
— a typo'd `htps://` — is a **fatal configuration error**, not an outage: the host
refuses to start rather than quietly falling back to a cached copy, because that
typo will never start working. Redirects are not followed, for the same reason the
scheme is checked at all: TLS to the address you named is the guarantee, and a 3xx
to another origin contradicts it.

### Rolling it out

Publish the document, point one host at it, verify, then widen.

```bash
# 1. Publish. Any HTTPS endpoint that serves the bytes verbatim: an S3 object
#    your hosts can read, a static web server, an internal config service. Put
#    any request credential in KIROCREW_POLICY_HEADERS rather than in the URL, so
#    it is not baked into a link that expires or leaks. ETag / Last-Modified
#    support is optional but saves a body on every unchanged poll.
aws s3 cp security_policy.json s3://corp-config/kirocrew/security_policy.json

# 2. Point the host at it, through whatever already sets environment variables
#    for the Kiro Crew service, then restart it once.
export KIROCREW_POLICY_URL=https://corp-config.s3.amazonaws.com/kirocrew/security_policy.json
export KIROCREW_POLICY_REFRESH_SECS=900
kirocrew restart

# 3. Verify. `source` reports the posture; `fetch` proves the round trip.
kirocrew policy source
kirocrew policy fetch --force
kirocrew policy show
```

`kirocrew policy source` reports whether central distribution is active, the
refresh interval, the staleness bound, the `on_unavailable` disposition, and how old
the cached copy on disk is. It prints the
source's **scheme, not its URL** — as do the dashboard's policy viewer and the
audit log — because the command is reachable from a shell the agent may drive and
the endpoint is your control plane. Read the URL from your own configuration, out
of band.

Its "polling now" and "last refresh" lines describe **the process you ran it in**.
A one-shot CLI run has no background poller, so it reports none even on a host
whose gateway is polling happily; the live refresher's own state is on
`GET /api/governance/policy` and in the dashboard's security panel.

`kirocrew policy fetch` fetches now, validates the document, and on success
installs it and records it as this host's last-known-good. Run from a shell, what
outlives the command is the validation and the cache write — the install lands in
that short-lived CLI process, and the running gateway takes the change on its own
next poll, or immediately at its next start from the cache the fetch just wrote.
The command says which of those applies, because with a **boot-only** source (no
`refresh_interval_secs`) there is no next poll: a gateway already running keeps its
ceiling until it is restarted. Set a refresh interval if a push has to bind
without one.
It **exits non-zero** when the document is refused or the source cannot be
reached, so it works unchanged as a verification step in a config-management run:
the host that did not take your change fails the run instead of printing a warning
into a log nobody reads. `--force` skips the cached ETag / Last-Modified
validators, because a `304` tells you nothing about whether the document you just
published reads correctly.

`kirocrew policy show` then prints the ceiling actually in effect, and
`kirocrew policy validate` load-checks the policy plus every profile. No `policy`
subcommand is exposed as an MCP tool, deliberately: the governed subject does not
get to enumerate its own ceiling.

### When the source is down

The cached last-known-good copy is served, and that is the ordinary answer — a
fleet does not lose its ceiling because a bucket had a bad minute. The cache lives
in `<data home>/policy_cache/` and is protected exactly as the policy file is: the
agent can neither read nor write it. A copy recorded against a *different* source
is ignored, so repointing a host at a new endpoint cannot be undone by the old
endpoint's cache. Falling back to it is itself recorded as a degradation, so the
dashboard shows which hosts are running on a cached ceiling rather than a fetched
one.

`max_cache_age_secs` is the staleness bound. `0` (the default) means a host that
fetched successfully once will keep running on that copy indefinitely; a positive
value says how long that is acceptable for. Past the bound, and when there is no
cached copy at all, `on_unavailable` decides:

| `on_unavailable` | Behaviour with no usable cache |
|---|---|
| `fail_closed` (**the default**) | **Kiro Crew refuses to start.** A fleet that pointed a host at a central ceiling meant that ceiling to bind, so "we could not reach it" must not read as "run unbounded". |
| `degrade` | Falls through to the next policy tier (a local `security_policy.json`, or none) and records a governance incident, so the dashboard shows the host as degraded. |

Be clear-eyed about the default: **a host with a cold cache and an unreachable
endpoint does not boot.** That is the intended behaviour, and it is also the
failure you are most likely to meet — a brand-new host provisioned while the
endpoint is misconfigured, or a container built with the variable set and no
cache baked in. The error message names the three levers, all of which take
effect on the next start:

- `KIROCREW_POLICY_ON_UNAVAILABLE=degrade` — boot, and report the degradation.
- unset `KIROCREW_POLICY_URL` — stop fetching centrally on this host.
- `KIROCREW_SECURITY_POLICY=/path/to/local.json` — govern from a local file,
  which outranks the central tier entirely.

A refusal to establish the ceiling aborts every `kirocrew` command on that host,
`policy source` included, because each of them boots the same platform context.
`kirocrew doctor` is the exception and the diagnostic to reach for: it is exempt
from the abort precisely so the one command that can explain the failure is not
bricked by it.

If you cannot tolerate a non-booting host, set `degrade` fleet-wide and watch the
governance indicator instead. That is a real trade, not a workaround: a degraded
host runs under whatever local policy it has, which may be none.

### Rolling back a bad push

One document governing every host is the widest blast radius in this model, so
plan the retraction before the first rollout.

`KIROCREW_SECURITY_POLICY` — an explicit **local** file path — outranks the
central tier and is the retraction lever. It is reachable without fixing the
endpoint, which is the point: an operator recovering from a bad push needs a
channel that outranks the thing that broke. Keep a known-good policy on each host
(or in your host image) so setting one variable is the whole recovery.

A running fleet is better protected than a restarting one, and the difference
matters when you plan:

- **On a live refresh, a bad document is refused and the running ceiling is
  kept.** A candidate is validated through the same gates boot uses before it is
  installed, so a refresh can never install a ceiling this host would have
  refused to start under. A refused document is not cached either, so a rejection
  does not outlive the push once you correct it. `kirocrew policy source` reports
  the refusal as the last refresh status.
- **A host that RESTARTS while a bad document is published will try to adopt
  it**, and at boot there is no running ceiling to fall back to. Under
  `fail_closed` that host does not come up. Assume this happens — autoscaling, a
  crash loop, a scheduled reboot window — and stage a change to a canary host
  (with `KIROCREW_POLICY_URL` pointed at a canary object) before you publish it
  to the fleet's URL.

### Signing, and how far it goes

A fetched policy may carry a detached signature in its `identity` block. Making a
**verified** signature mandatory is one switch, and it is deliberately not in the
policy: `require_policy_signature` in the operator-controlled
`admission_policy.json`, which demands one on *every* policy tier, the fetched one
included.

The trust key lives in that same file, under `trust_keys` keyed by the policy's
`identity.issuer`. Both live there rather than in the policy for the same reason: a
document must not be the authority on whether it has to be authentic, since an
attacker rewriting the policy would simply clear such a flag — and
`admission_policy.json` is on the protected floor the agent cannot write.

Coverage is the whole document minus the signature, `identity.issuer` included, so a
signed policy cannot be re-labelled as issued by someone else, and re-indenting the
file does not invalidate it while changing any value does.

Two limitations, stated plainly:

- **The primitive is symmetric HMAC-SHA256.** Any host that can verify a
  signature holds a secret that can also *produce* one, so this detects an
  endpoint or transport that tampered with the document — not a host that decided
  to forge its own. It raises the bar; it is not a public-key attestation. An
  asymmetric verify swaps in behind the same helper if that changes.
- **No signing runbook ships.** There is no `kirocrew policy sign`, no key
  distribution tooling and no rotation procedure; you compute the signature and
  place the key yourself. `require_policy_signature` on a fleet with no matching
  trust key means every policy is refused, so place the key first. Leaving it
  `false` — as [the example policy](assets/security-policy.example.json) does — is
  a reasonable starting point when the endpoint is already an authenticated,
  TLS-fronted internal service.

### What is not included

So you do not plan around capabilities that are not here:

- **No MDM or directory integration.** Kiro Crew reads an environment variable
  and an HTTPS URL. Jamf, Intune, Group Policy, Ansible and friends are how a
  host gets pointed at a source; nothing on this side knows about them.
- **No fleet-compliance reporting.** There is no console that lists which hosts
  adopted which version. Each host reports only its own posture, over
  `kirocrew policy source`, the dashboard's policy viewer, and its own audit log
  (which records refresh outcomes that changed something or failed, by scheme, not
  by URL). Aggregation is yours to build — `kirocrew policy fetch`'s exit code is
  the intended hook.
- **No push channel.** As above: a change lands within one refresh interval, not
  instantly, and the interval has a 60-second floor.
- **No staged or percentage rollout.** One URL serves one document to everyone
  who reads it. Canarying means publishing to a second object and pointing a few
  hosts at it with `KIROCREW_POLICY_URL`.
- **Nothing is distributed except the policy.** Profiles, the admission policy
  (including the trust keys), `config.json` and agent configuration are all still
  per-host.

## Related

- [../architecture/mcp.md](../architecture/mcp.md) — how Kiro Crew composes the
  agent spec's `mcpServers` map and which files it owns.
- [../system-specs/modules/governance.md](../system-specs/modules/governance.md)
  — the full `security_policy.json` reference: every governed scope, the
  policy-versus-profile algebra, and the distribution engine's internals.
- [assets/security-policy.example.json](assets/security-policy.example.json) — a
  policy with a `distribution` block filled in, to copy from.
- [../../src/kiro_crew/docs/troubleshooting.md](../../src/kiro_crew/docs/troubleshooting.md)
  — the user-facing "MCP tools not working" checklist.
- Kiro's own documentation: `https://kiro.dev/docs/enterprise/governance/mcp/`
  (administrator setup) and `https://kiro.dev/docs/mcp/registry/` (registry mode
  and registry-type overrides).
