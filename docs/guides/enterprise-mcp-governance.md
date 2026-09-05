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
- **The document is the whole ceiling, not a patch — but it is one rung of a
  ladder.** The fetched policy replaces the *previously fetched* one outright; there
  is no merge of two central documents and no per-host addendum that can loosen what
  you published. What it does not replace is the rest of the ladder: a managed
  profile above it still outranks it, and a local `security_policy.json` below it
  still tightens it. That holds identically at boot and on every refresh, because
  both run the same composition — so a host you tightened locally does not quietly
  lose that tightening at its next successful poll.
- **The fetched document outranks every local file.** A local policy — including one
  named by `KIROCREW_SECURITY_POLICY` — can only *tighten* what you published; it
  cannot loosen a single clause. That reverses the older behaviour, where a local
  file beat the central one and the fleet ceiling was therefore advisory: anyone who
  could set an environment variable could point it at a permissive file. The one
  thing above the fetched document is
  [the managed tier](#the-managed-tier-a-ceiling-a-standard-user-cannot-touch), and
  the one way a local file can override rather than narrow is a dated grant you write
  into the published document yourself — see
  [Rolling back a bad push](#rolling-back-a-bad-push).

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

Both of these say *where the document comes from*, not *whether it binds*. Neither
is a channel a standard user cannot touch: an environment variable is per-process
and redefinable by whoever launches the process, so your MDM can **set** one but
never **pin** one, and a bootstrap policy file lives in a directory the user may
own. If you need the ceiling itself to be untouchable, place it in
[the managed tier](#the-managed-tier-a-ceiling-a-standard-user-cannot-touch) as
well — the two work together, since a managed document may carry the
`distribution` block that names your source.

When it does, that source is **pinned**: the refresh, timeout, cache-age and
on-unavailable variables are ignored on that host, and so is `KIROCREW_POLICY_URL` once
the managed block names a `source`, so nobody who can set a variable can point it at a
document of their own — which is the whole reason to put the block there. If you pin
only the cadence and leave the `source` out, `KIROCREW_POLICY_URL` still supplies the
address: it is the only channel left that can, and disregarding it would switch central
distribution off on that host rather than harden it. They are ignored rather than
rejected, so setting one does not
stop the host from starting; you get a warning naming the variables, never their values
since a URL can carry a token, and a governance audit record. If your managed profile
says nothing about `distribution` at all, none of this applies to that host and every
`KIROCREW_POLICY_*` variable works as it always did — there is no fleet choice to
protect until you publish one. `KIROCREW_POLICY_HEADERS`
still works, because that credential is per-machine by design and is not your fleet's
choice of document. A block in any other tier's file keeps the per-setting environment
override described above.

The full order Kiro Crew loads in, highest first:

| Tier | Where | Role |
|---|---|---|
| 1 | the MDM-managed configuration profile | **authority** — the only channel a standard user cannot write |
| 2 | the centrally distributed document | **authority** — one document, every host |
| 3 | `KIROCREW_SECURITY_POLICY` | subordinate — tightens only |
| 4 | a companion edition's packaged policy | subordinate — tightens only |
| 5 | `~/.kiro/crew/security_policy.json` | subordinate — tightens only |

A `distribution` block is read from the first of those tiers that supplies one — the
managed profile, then `KIROCREW_SECURITY_POLICY`, then a companion's packaged policy,
then `~/.kiro/crew/security_policy.json` — so naming a source in the file
`KIROCREW_SECURITY_POLICY` points at works exactly as naming it in any other tier's
file.

Tiers 3–5 are mutually exclusive (the first one present is used) and the result is
the authority narrowed by it: allow-lists intersect, deny-lists union, a strictness
level takes the stricter value. A subordinate cannot repeal by omission either — a
scope it simply leaves out keeps the authority's value.

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
`KIROCREW_SECURITY_POLICY`, not this channel. The validator is a content digest,
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

`kirocrew policy fetch` fetches now, folds the document back into the tier ladder,
validates that composed result, and on success installs it and records the fetched
document as this host's last-known-good. Run from a shell, what outlives the command is
the validation and the cache write — the install lands in that short-lived CLI process,
and the running gateway takes the change on its own next poll, or immediately at its
next start from the cache the fetch just wrote.
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
- `KIROCREW_SECURITY_POLICY=/path/to/local.json` — supply a local ceiling so the
  host has one. Note what this is **not**: a local file no longer outranks the
  central tier, so on a host that *did* reach the endpoint it only tightens what you
  published. It is a way to give a host a ceiling, not a way to escape one.

A refusal to establish the ceiling aborts every `kirocrew` command on that host,
`policy source` included, because each of them boots the same platform context.
`kirocrew doctor` is the exception and the diagnostic to reach for: it is exempt
from the abort precisely so the one command that can explain the failure is not
bricked by it.

If you cannot tolerate a non-booting host, set `degrade` fleet-wide and watch the
governance indicator instead. That is a real trade, not a workaround: a degraded
host runs under whatever local policy it has, which may be none.

### The managed tier: a ceiling a standard user cannot touch

Everything above answers *where the document comes from*. This answers *who can
replace it*. An environment variable is per-process and redefinable by whoever
launches the process, and a policy file under the user's own data home is theirs to
edit — so neither is a channel your fleet can rely on. A **managed configuration
profile** is: it lands as a root-owned file that the MDM re-asserts on every
check-in, so a local edit is reverted rather than honoured, and it is the **highest**
tier, above every environment variable and every local file.

The tier is **inert when the file is absent**, which is every standalone install, so
adopting it changes nothing for hosts you have not targeted.

| Platform | Path Kiro Crew reads | How you write it |
|---|---|---|
| macOS | `/Library/Managed Preferences/dev.kirocrew.plist` | a configuration profile with preference domain `dev.kirocrew` |
| Linux | `/etc/kirocrew/security_policy.json` | config management (Ansible, Puppet, Chef, Intune for Linux) writing as root |

The document is the **same schema** as `security_policy.json` — the same `version`
and `boot`, the same governed scopes (`tools`, `commands`, `network`, `filesystem`,
`capabilities.*` …), and the same `updates`, `distribution`, `break_glass` and
`identity` blocks. [The example policy](assets/security-policy.example.json) is a
valid managed document as it stands. On macOS it is that document expressed as a
property list; on Linux it is the JSON verbatim. Kiro Crew picks the parser from the
file extension, so do not rename either one.

**Jamf Pro.** Computers → Configuration Profiles → New → **Application & Custom
Settings** → *External Applications*, source *Custom Schema* or *Upload*, with
**Preference Domain** `dev.kirocrew` and your policy as the payload. Scope it to the
smallest test group first, then widen. Jamf writes
`/Library/Managed Preferences/dev.kirocrew.plist` on the next check-in and rewrites it
on every subsequent one — that re-assertion is the property the tier depends on, so do
not deliver the file with a script instead.

**Intune (macOS).** Devices → Configuration → Create → macOS → Templates →
**Preference file**, with **Preference domain name** `dev.kirocrew` and the plist
uploaded as the property list file. Same result, same re-assertion.

**Linux.** Write the JSON as root. Ansible, for example:

```yaml
- name: install the Kiro Crew managed ceiling
  ansible.builtin.copy:
    src: security_policy.json
    dest: /etc/kirocrew/security_policy.json
    owner: root
    group: root
    mode: "0644"
```

**A file that fails the trust checks is REFUSED, not ignored.** Kiro Crew does not
fall through to a lower tier when the managed file looks wrong, because falling
through is exactly the override this tier exists to remove — so the host fails to
start and says why. What it requires:

- **owned by root** (uid 0). A file at the managed path owned by anyone else is a
  misconfiguration or an attempt, and neither should quietly widen the ceiling.
- **not group- or world-writable** — no `0o022` bits. `0644` is right; `0664` is
  refused, because anyone in the group could rewrite the fleet ceiling.
- **a regular file, not a symlink.** The open uses `O_NOFOLLOW`, so a symlink planted
  at the managed path is refused rather than followed.
- **at most 1 MiB**, and parseable as a plist (macOS) or JSON (elsewhere) **object**.
- **openable by the account Kiro Crew runs as.** Ownership and mode govern who may
  *write* the file; Kiro Crew still has to read it, so `0644` under a traversable
  directory is what satisfies both. Tightening the file to `0600`, or the directory to
  root-only, on a host where Kiro Crew does not run as root fails the start with the
  path in the message rather than falling through to a lower tier — that fall-through is
  what an earlier existence pre-check did when the directory denied it, quietly
  disabling the ceiling the hardening was meant to protect.

**There is no managed tier on Windows.** A Windows host reads no managed document at
all — not a refused one, an absent one — so treat the managed profile as a macOS and
Linux control and give a Windows fleet its ceiling through
[central distribution](#central-policy-distribution-one-security-policy-every-host)
instead. The reason is that the plausible location, `%ProgramData%\KiroCrew\`, is
resolved from an environment variable the launching user controls, and the checks above
cannot make up for it: Windows has no uid to compare, so the ownership and mode tests
do not apply and only the regular-file test would remain. That combination would let a
standard user point the **highest** authority at a file they wrote, which is worse than
having no managed tier — so the tier is absent until it can be built on the
non-overridable known-folder API with a real ACL check, which is not in this release.

If a host refuses to start, `kirocrew doctor` is exempt from the abort and will name
the reason.

**How far this goes — stated plainly.** None of this binds a user with **local root**.
Root can rewrite the managed file, clear the opt-in in `admission_policy.json`, or
edit the installed Python — the wheel installs readable `.py` files and hiding them
would not be a boundary. What you get is worth having anyway:

- a **standard user cannot loosen the ceiling at all** — there is no tier they can
  write that widens it; and
- an **admin who edits the managed file** has that edit reverted by the MDM on its
  next check-in, and the change is in the audit trail.

Making the ceiling hold against root needs server-side attestation — the client
proving which policy digest it loaded, with no service on a mismatch. That is not
built, and it is not a thing you can configure your way to today.

### Signing with a public key you publish

Ownership of the managed file proves a standard user did not write it. A signature
proves **you** authored the bytes, which is the other half — and it is the half that
covers a fetched document, a compromised distribution endpoint, or a file swapped in
transit before the MDM wrote it. Kiro Crew verifies **Ed25519** and the trust root
holds only the **public** half, so reading a host confers no ability to forge a
ceiling.

Generate a key pair once, and keep the private half off every managed host:

```bash
openssl genpkey -algorithm ed25519 -out policy-signing.key      # keep this OFF the fleet
openssl pkey -in policy-signing.key -pubout -outform DER | tail -c 32 | base64
```

Publish the public half in `admission_policy.json`, keyed by the policy's
`identity.issuer`, alongside the two opt-ins:

```json
{
  "require_policy_signature": true,
  "require_asymmetric_policy_signature": true,
  "trust_public_keys": {
    "corp-security": "kEo0…base64 or hex, the 32 raw bytes…="
  }
}
```

- **`trust_public_keys`** is checked **before** the legacy symmetric `trust_keys`, so a
  fleet migrating can carry both during the rollout and have the strong proof win per
  issuer. Base64 (padding optional) or hex is accepted — whichever your tooling emits.
- **`require_policy_signature`** demands a verified signature on **every** tier: the
  managed profile, the fetched document, and any local file. Place the key before you
  set it, or every policy is refused.
- **`require_asymmetric_policy_signature`** additionally refuses to accept a symmetric
  HMAC verdict as proof. Set it once you have finished migrating; a fleet already
  signing with a shared secret must not have its ceiling invalidated by an upgrade.

Both flags and the key live in `admission_policy.json` — or in the file
`KIROCREW_ADMISSION_POLICY` names — rather than in the policy, because a document must
not be the authority on whether it has to be authentic: an attacker rewriting the
policy would simply clear such a flag. That file is on the protected floor the agent
cannot read or write, and it is **per-host**: nothing distributes it, so place it with
the same config management that places the managed profile.

**You cannot deliver the key in the configuration profile that carries the ceiling.**
There is no policy-side key at all: a policy document containing `trust_public_keys` is
**refused**, because an unknown top-level key fails closed. `admission_policy.json` is
the only trust root, and it is placed as its own file.

Write the flags as real JSON **booleans**, not strings.
`"require_asymmetric_policy_signature": "false"` — an easy mistake in a hand-edited or
templated file — is dropped with a warning rather than read as `false`, because reading
it as truthy would silently turn the requirement **on** and reject every correctly
HMAC-signed ceiling you publish.

**What signing does not buy, stated plainly.** The trust root is a file in the user's
own data home, and an environment variable can point Kiro Crew at a different one — so
a standard user on the host can clear `require_policy_signature` and
`require_asymmetric_policy_signature`, in place or by redirection. Signing binds a user
who does not edit their own trust root: it makes a document you published
tamper-**evident** to a host that loads it, and it does not make the requirement itself
unclearable on that host. Protecting the trust root is a separate change of comparable
size to central distribution, and it is not built.

Coverage is the whole document minus the signature, `identity.issuer` included, so a
signed policy cannot be re-labelled as issued by someone else, and re-indenting the
file does not invalidate it while changing any value does. There is still **no signing
runbook**: no `kirocrew policy sign`, no key distribution tooling, no rotation
procedure. You compute the signature over the canonical form (sorted keys, compact
separators, UTF-8) and place the key yourself.

### If you leave signing off

Verification is **advisory by default**: with `require_policy_signature` unset — as
[the example policy](assets/security-policy.example.json) leaves it — an unsigned
policy still loads and still governs, at every tier, with no key to provision. That
is a reasonable starting point when the endpoint is already an authenticated,
TLS-fronted internal service and the managed profile is delivered by an MDM you
trust; what you give up is detecting a document that was altered between your hand
and the host's. `kirocrew policy show` prints the verdict, so you can see which
of the two you are running.

### Rolling back a bad push

One document governing every host is the widest blast radius in this model, so
plan the retraction before the first rollout.

**The lever is a grant you write into the ceiling, not a variable you set on the
host.** `KIROCREW_SECURITY_POLICY` no longer outranks the central tier — it can only
tighten it — because while it did, the fleet ceiling was advisory: any account that
could set an environment variable could point it at a permissive file. What replaces
it is `break_glass`, a dated block in an **authority** document (the managed profile
or the published one) naming the lower tier it releases:

```json
"break_glass": {
  "tiers": ["env"],
  "expires": "2026-09-30T00:00:00Z"
}
```

While that grant is live, a file named by `KIROCREW_SECURITY_POLICY` **replaces** the
authority outright rather than narrowing it — which is what recovery requires, since
intersecting cannot undo a clause that is too strict. Three things to know before you
rely on it:

- **`expires` is required.** A block with no parseable expiry grants nothing. An
  undated grant is a permanent hole that outlives the incident it was opened for, so
  the fail-closed direction is also the one an operator who forgets to remove the
  block wants. Bare dates are read as UTC.
- **`tiers` accepts only `env`, `bundled` and `home`.** `managed` and `central` are
  not valid — they *are* the authority, so "grant yourself an override" is not
  expressible. A misspelled tier name is **refused**, not ignored, so you find out
  when you publish rather than during the incident.
- **Every use is loud, and the record comes first.** Pulling the lever writes a
  `security_policy_break_glass` audit record naming both tiers and logs a warning
  carrying the expiry, so an operator reading logs during an incident sees that a
  local document is in charge. If that record cannot be written the override is
  **refused** and startup fails naming both tiers, rather than the rollback taking
  effect unrecorded — so a host that cannot write its own audit trail is a host where
  the lever does not work, and that is deliberate. Ordinary tightening is unaffected:
  it is audited best-effort and an unwritable log never blocks it.

That has a consequence for how you plan: **the grant has to be published before you
need it.** A bad push that omits `break_glass` leaves nothing on the host that beats
it, so carry the block in the document you publish (with a rolling expiry) if you want
one-variable recovery, or be prepared to fix the endpoint and let hosts re-fetch.
Either way, keep a known-good policy on each host or in your host image.

A live grant also stands the poller down, so your rollback is not overwritten a minute
later: `kirocrew policy fetch` refuses before spending a request and says why, each
background cycle refuses on the same test before spending one, and an install would be
refused anyway as a last guard. The loop itself keeps **running** — it is the expiry
that ends the rollback, and a loop that had stopped would not be there to notice it, so
a dated grant would have become a permanent one until the host restarted. Because the
refusal is decided before the fetch, a cycle under a live grant costs no request, and
the first cycle after the grant lapses re-fetches and reinstates the fleet ceiling with
no operator action. A local file **without** a grant does none of that — it is merely a
subordinate, and refreshes carry on installing the authority it tightens.

The stand-down covers both states you can be in, and every tier a grant can name.
**After a restart** the ladder has recomposed and the installed ceiling *is* the
released rollback, carrying the grant that released it — so the poller recognises the
lever by asking which tier the running ceiling came from, which is what makes a granted
`home` or `bundled` rollback stand it down exactly as an `env` one does. **Before a
restart**, when you have just dropped the rollback document and nothing has recomposed
yet, it instead asks whether the live grant names `env` or `home` and whether such a
file is there now — that is precisely the moment a poll would install over the lever you
just pulled, so it is checked rather than assumed. A `KIROCREW_SECURITY_POLICY` rollback
is additionally required to exist, since a variable can outlive the file it names; a
packaged or home document is not.

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

### What is not included

So you do not plan around capabilities that are not here:

- **No directory integration, and MDM only as a file.** Kiro Crew reads a managed
  configuration profile at a fixed per-platform path, an environment variable and an
  HTTPS URL. Jamf, Intune, Group Policy and Ansible are how that file arrives and how
  a host gets pointed at a source; nothing on this side talks to them, enrols with
  them, or knows whether a host is enrolled at all.
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
  (including `trust_public_keys` and the two signature opt-ins), `config.json` and
  agent configuration are all still per-host — place them with the same config
  management that places the managed profile.

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
