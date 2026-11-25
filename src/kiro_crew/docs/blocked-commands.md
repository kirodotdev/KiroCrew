# Blocked commands and credential access

Kiro Crew gives an agent real tool access, so some commands are refused at the
runtime boundary rather than by asking the model to behave. This page explains
what gets refused, what the agent is told to do instead, and how to check your
own credential setup when something looks unavailable.

## The most common false alarm: "the agent says it has no AWS access"

Reading credential files is blocked. **Running AWS CLI commands is not blocked by that rule.**

Those two facts are easy to conflate. An agent that tries to read
`~/.aws/config` to discover your profile names gets refused, and could wrongly
report that the host has no AWS access at all — while `aws sts
get-caller-identity` works the whole time.

The refusal carries the sanctioned path with it, so the agent is told to run
`aws configure list-profiles` and `aws sts get-caller-identity` instead of
reading the file. If you still see that conclusion, run `kirocrew doctor` —
its **Credentials** section reports whether anything is actually configured.

## What is refused, and why

| Class | Examples | Why |
|---|---|---|
| Credential files | `~/.aws`, `~/.ssh`, `~/.gnupg`, `~/.netrc`, `~/.npmrc`, `~/.git-credentials`, `~/.config/gcloud`, `~/.azure`, `~/.docker/config.json`, `~/.kube/config` | The bytes are a bearer credential. An agent that can read them can act as you anywhere they are accepted. |
| Enterprise SSO session | the SSO cookie store on a corporate host | A live session token. Fenced for reading as well as writing, so it cannot be copied into a cookie jar either. |
| The governance trust root | `security_policy.json`, `profiles/`, `admission_policy.json`, `denied_commands.json` | This is the ceiling the agent is governed by. File tools and the tool gate refuse these files, and the OS sandbox makes them read-only to agent code, so the agent cannot write (and so cannot disable) its own ceiling. Reading them is allowed by design. |
| Exfiltration shapes | `curl -d @file`, `wget --post-file`, `/dev/tcp/` redirects | Reading a local file into an outbound request body is indistinguishable from exfiltration, whatever the destination. |
| Destructive operations | `rm -rf /`, `terraform destroy`, `TRUNCATE TABLE`, `aws … delete-*` | Irreversible. These are disable-able if you want them (see below); the credential ones are not. The SQL rules match the statement's text anywhere on the line, so a heredoc or `printf` that merely writes the words is refused too — see **Statement text, not just statements** below. |
| Self-protection | minting a dashboard token, killing the gateway, an inline interpreter that imports Kiro Crew | Prevents the agent from escalating its own access or shutting down its supervisor. |
| ssh to this machine | `ssh` to `localhost`, `scp`/`sftp`/`rsync` to a loopback address or one of this host's own names; `git` or `tar` over an ssh transport to the same targets (`git clone localhost:repo`, `git fetch ssh://127.0.0.1/repo`, `tar -cf localhost:out.tar dir`, or a `GIT_SSH_COMMAND`/`core.sshCommand`/`ext::`/`--rsh-command` value that dials this host) | sshd runs outside the agent sandbox, so a connection back to this host is an unsandboxed login shell. A target not yet classified as remote is refused too. Local `git` work, a clone of a local path or an http(s) URL, a fetch from another host and a local `tar` archive are not affected. |
| Very deep nesting | a command with more than 32 `$( )`, `<( )` or `>( )` substitutions open inside each other | Each rule re-reads every nested body, so the cost grows with the depth; past 32 no rule can read the command in bounded time. The refusal says `substitution nesting deeper than 32` and asks for a flatter command. No session override waives it. Backticks and plain `( )` groups do not count. |

The full built-in rule list, with a human-readable description per rule, is in
the dashboard under **Settings → Security**.

## What this tier cannot see

The rules read the command line a tool call carries. That includes a program
spelled out **on** the line, which is how the inline-interpreter rule in the
table above works. What it does not include is a body the line merely **names**:
a program held elsewhere is never opened, so a command carried inside one is not
matched, even where the same command written on the line is refused.

That limit is deliberate, and it settles what the tier is: **friction against a
direct command line, not a security boundary.** A pattern you add here raises
the cost of the obvious spelling. It does not fence the capability off, and no
number of extra patterns closes the gap, because the gap does not care which
commands are named. Closure belongs to the layer below, which sees a process
however it was spelled — the OS-level fence of the sandbox
([configuration.md](configuration.md) covers the sandbox modes).

Two consequences worth planning around:

- Read this tier as defense in depth, not as the control of record. For anything
  irreversible, the control of record is the capability itself — which
  credentials a session can reach, and what the sandbox lets a process do.
- The audit trail carries the same limit. The bash audit hook records the command
  line the tool ran, so a wrapped command is logged as its wrapper.

## Statement text, not just statements

The mirror image of the limit above: everything that IS on the line is read as
the line. The three SQL rules (`sql-drop-database`, `sql-drop-table`,
`sql-truncate-table`) match the statement's text wherever it appears on the
command line — quoted, as an `echo` or `printf` argument, inside a heredoc body,
or in a `python -c` program — because a command line cannot tell a statement a
database client is about to run from one that is only being written down, and
the rule refuses rather than guess. So a shell command that writes release notes
mentioning `DROP TABLE` is refused exactly like one that runs it, and re-spelling
the body meets the same rule.

This is deliberate and the match is not narrowed. What changed is that the
refusal now says so: the agent is told the rule matched text, that a statement
meant to run belongs to you, and that prose or data is written with the
file-editing tool, which is not a shell command and is not read by this rule,
then handed by path to a prose reader (`gh … --body-file <path>`, `git commit -F
<path>`) and never to a database client. If the agent reports that a note it was writing was refused, that is the expected
reading of this rule, not a broken policy.

## What the agent is told

A refusal reaches the agent as two things: the rule that fired, and — for the
classes above where the next step is not obvious — the sanctioned path to what
it was trying to do. That second half is why the agent should not stall, retry
the same command under a different reader, or tell you that *you* cancelled it.

One refusal is the exception that IS retried: an ssh-family command refused as
**PENDING** because its target is still being classified (a background DNS check
for a new dotted hostname, this machine's address list still loading, a large
hosts file still being read). The agent is told to wait a few seconds and retry
the exact command; it runs once the target is classified as remote.

If the agent ever claims you denied something, that is a bug worth reporting:
the block came from the host, not from you.

## Checking your own setup

```bash
kirocrew doctor
```

The **Credentials** section reports:

- whether an AWS config or credentials file exists, and which profiles it names.
  When the host refuses to check `~/.aws` (an agent sandbox that hides it, or a
  `~/.aws` you lack permission on), it says it could not check and why, tells you
  to run `kirocrew doctor` from your own terminal or check the folder's
  permissions, and the rest of the report still runs
- whether a `credential_process` entry is configured (short-lived, auto-refreshing
  credentials — the recommended setup)
- whether this host mounts an MCP server that vends credentials directly, which
  some managed and enterprise editions install
- on Linux, whether sandboxed agents can see your Kerberos ticket. The sandbox
  starts each agent in its own user namespace, and the kernel keeps
  `KEYRING:persistent:` and `KEYRING:user:` ticket caches per user namespace
  (`KEYRING:persistent:%{uid}` is the default on some distributions), so there
  the cache is a different, empty keyring: MCP servers that need a ticket
  report none while `klist` in your terminal shows one. A file cache works on
  both sides. Run `kinit -c FILE:/tmp/krb5cc_$(id -u)`: Kiro Crew points agents
  at that file automatically, and it is not renewed for you, so re-run it when
  the ticket expires. Or, host-wide for every user, set
  `default_ccache_name = FILE:/tmp/krb5cc_%{uid}` in the `[libdefaults]`
  section of `krb5.conf`; after that change every user runs `kinit` again. Each
  Kiro Crew process also logs this once as a warning. The row reads the
  environment of the shell `doctor` runs in, which can differ from the
  gateway's.

Nothing in that section reads a secret value, and it never fails the overall
`doctor` run — a missing AWS setup is not a Kiro Crew fault.

## Finding who was refused what

Every tool call, governance decision and dashboard or API access is recorded in
the security event log, `security_events.jsonl` in the config directory. A row
holds the time, event type, caller identity, agent, source (`dashboard`, `slack`,
`cron`, ...), operation, outcome, the resources and error text, and the hash
that chains it to the row before it. The signing key is kept outside the log
directory. `kirocrew security events` prints the newest 20 rows; the flags below
narrow them and export them.

| Flag | Keeps a row when |
|---|---|
| `--operation X`, `--outcome X`, `--source X`, `--caller X` | that field equals `X` exactly (`--caller` reads the caller identity) |
| `--since T`, `--until T` | its time is at or after `--since` and before `--until` |
| `-n N` | at most `N` rows are shown (default 20, counted after the filters) |
| `--format F` | `text` (default), `jsonl` or `csv` |
| `--out FILE` | writes a `jsonl` or `csv` export to `FILE` and `FILE.manifest.json` |

Flags combine, and a row must match every field filter. A value is compared as
whole text, never as a pattern, and case matters: `--outcome Denied` finds
nothing. A time is an age (a number and `s`, `m`, `h`, `d` or `w`, such as `7d`)
or an ISO 8601 instant; one without an offset is read as UTC. An unreadable time,
or a `--since` that is not earlier than `--until`, prints an error and exits 2.
A field filter looks at the newest 50,000 rows of the window. If that budget ran
out first, standard error says it looked at the newest 50000 rows only; an
earlier `--until` reaches older rows.

### Export

`--format jsonl` writes each row as stored, one JSON object per line. `--format
csv` writes a header and one row per event in a fixed column order (`timestamp`,
`event_id`, `event_type`, `caller_identity`, `agent`, `source`, `operation`,
`tool_kind`, `outcome`, `resources`, `downstream_service`, `request_id`, `error`,
`metadata`, `prev_hash`, `entry_hash`). Control characters other than tab,
newline and carriage return are removed from CSV cells, and a cell that starts
with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'` so a
spreadsheet does not run it as a formula. An export holds every match, oldest
first, unless you give `-n`, which keeps the newest `N`. An unknown format prints
a warning and shows text. `--out` with `text` is ignored with a warning.

Without `--out`, the export goes to standard output and a `rows=N first=...
last=...` line goes to standard error. With `--out FILE`, two new files are
created, readable by the owner only: `FILE` and `FILE.manifest.json`. Neither is
ever overwritten. If either already exists, the command exits 1 with `could not
write FILE: it or its manifest already exists; nothing written`. Any other write
failure also exits 1 and deletes whatever it had created, so no half export is
left. The messages name the file, not its full path.

The manifest records `format`, `exported_at`, the `filters` given, `since` and
`until`, `rows`, `first_timestamp`, `last_timestamp`, `chain_hash_first`,
`chain_hash_last`, `scan_capped` and `sha256`. What each lets you check:

- `sha256` is the SHA-256 of the export file's text. `sha256sum FILE` matching it
  shows the file has not changed since the export. It is an unkeyed hash, so it
  does not stop someone who rewrites both files.
- `chain_hash_first` and `chain_hash_last` are the `entry_hash` of the first and
  last exported rows (`null` for an empty export). Compare them with the `entry_hash`
  of the same rows in the log to show the ends of the export are real log entries.
  `kirocrew security verify` checks the whole chain in the log itself. A rotated
  segment starts a new chain, so an export that spans one has ends in two chains.
- `scan_capped: true` means older matching rows were not looked at, so the
  export may be incomplete.

### Examples

```bash
kirocrew security events --outcome denied --caller dashboard:alice --since 7d -n 50
kirocrew security events --outcome denied --since 7d --format csv --out denied-week.csv
sha256sum denied-week.csv && grep '"sha256"' denied-week.csv.manifest.json
```

The first prints one caller's refusals from the last week. The second writes
every refusal from the week to a spreadsheet file plus its manifest, and the
third compares the file's hash with the manifest's.

## Adjusting the rules

**Settings → Security** lets you disable individual built-in rules, disable them
all, and add, edit and sort your own patterns. When you add your own rule, fill
in the **note** field: that note is what the agent is shown when your rule fires,
instead of a raw regex it has to guess the intent of.

Two limits are deliberate:

- The agent cannot edit any of this. The files live on the sensitive-path floor,
  which is what stops a prompt-injected agent from rewriting its own ceiling.
- On a host with an enterprise governance policy, every built-in rule that
  policy pins (a `commands`-scope deny whose pattern is the rule's own regex, for
  example credential, self-protection or git-publish rules) is **pinned** and
  cannot be disabled — including by "disable all". Which rules are pinned is the
  policy's choice, and `kirocrew policy show` lists them. Your organization's
  policy composes with your settings on a tightest-wins basis.

Both are about what you may change. What the rules can *see* is a separate
limit, and the tighter one: see **What this tier cannot see** above before you
treat a pattern here as a fence.

To see the ceiling on such a host:

```bash
kirocrew policy show
kirocrew policy explain <scope> <item>
kirocrew policy explain <scope> <item> --json
kirocrew policy explain --batch checks.jsonl
```

`--json` prints the answer as one JSON object. `--batch FILE` (`-` reads stdin)
answers many checks in one run, which is how a script tests profiles kept in git:
each line is a JSON object with `scope`, `item` and optional `session_key`,
`agent` and `app` (the flags fill in what a line leaves out; other keys, such as
your expected verdict, are ignored), and each answer is one JSON line with the
request's `line` number, those inputs, and `verdict`, `profile`, `rule`, `layer`,
`reason`, `explicit_deny`, `gate_verdict` and `gate_reason`. A line it cannot
read, or an unknown scope, prints `{"line": N, "error": ...}` and the command
exits 1 at the end.

To test a profile, assert `verdict`: it answers that one scope-and-item question
the way the live gate answers each scope and item a tool call names. The gate is
looser in two places, and only for a DENIED that means "not in the allow list"
(`explicit_deny: false`): a built-in tool it knows by more than one name is let
through when each layer lists one of its names, and a call it knows is not a shell
command is refused outside its own scopes, `commands` included, only by a deny rule.
`explicit_deny: true` means a deny rule matched, which the gate refuses in every
case. `gate_verdict` ignores the scope and reads the item as a tool's display title
with no arguments (`Running: <command>`, `Reading <path>`, `mcp__<server>__<tool>`,
or a bare name checked as both a command and a tool). Neither covers the refusals in
**What is refused, and why** above, which are checked before governance.

These are CLI-only on purpose: the governed agent does not get a tool for
enumerating its own ceiling.

## Related

- [configuration.md](configuration.md) — sandbox modes and config reference
- [troubleshooting.md](troubleshooting.md) — general problems and fixes
