# Blocked commands and credential access

Kiro Crew gives an agent real tool access, so some commands are refused at the
runtime boundary rather than by asking the model to behave. This page explains
what gets refused, what the agent is told to do instead, and how to check your
own credential setup when something looks unavailable.

## The most common false alarm: "the agent says it has no AWS access"

Reading credential files is blocked. **Running AWS CLI commands is not.**

Those two facts are easy to conflate, and the agent used to conflate them: it
would try to read `~/.aws/config` to discover your profile names, get refused,
and report that the host had no AWS access at all — while `aws sts
get-caller-identity` would have worked the whole time.

The refusal now carries the sanctioned path with it, so the agent is told to run
`aws configure list-profiles` and `aws sts get-caller-identity` instead of
reading the file. If you still see the old conclusion, run `kirocrew doctor` —
its **Credentials** section reports whether anything is actually configured.

## What is refused, and why

| Class | Examples | Why |
|---|---|---|
| Credential files | `~/.aws`, `~/.ssh`, `~/.gnupg`, `~/.netrc`, `~/.npmrc`, `~/.git-credentials`, `~/.config/gcloud`, `~/.azure`, `~/.docker/config.json`, `~/.kube/config` | The bytes are a bearer credential. An agent that can read them can act as you anywhere they are accepted. |
| Enterprise SSO session | the SSO cookie store on a corporate host | A live session token. Fenced for reading as well as writing, so it cannot be copied into a cookie jar either. |
| The governance trust root | `security_policy.json`, `profiles/`, `admission_policy.json`, `denied_commands.json` | This is the ceiling the agent is governed by. Its unreachability from a tool call is what makes the ceiling un-disableable. |
| Exfiltration shapes | `curl -d @file`, `--upload-file`, `wget --post-file`, `/dev/tcp/` redirects | Reading a local file into an outbound request body is indistinguishable from exfiltration, whatever the destination. |
| Destructive operations | `rm -rf /`, `terraform destroy`, `TRUNCATE TABLE`, `aws … delete-*` | Irreversible. These are disable-able if you want them (see below); the credential ones are not. |
| Self-protection | minting a dashboard token, killing the gateway, an inline interpreter that imports Kiro Crew | Prevents the agent from escalating its own access or shutting down its supervisor. |

The full built-in rule list, with a human-readable description per rule, is in
the dashboard under **Settings → Security**.

## What the agent is told

A refusal reaches the agent as two things: the rule that fired, and — for the
classes above where the next step is not obvious — the sanctioned path to what
it was trying to do. That second half is why the agent should not stall, retry
the same command under a different reader, or tell you that *you* cancelled it.

If the agent ever claims you denied something, that is a bug worth reporting:
the block came from the host, not from you.

## Checking your own setup

```bash
kirocrew doctor
```

The **Credentials** section reports:

- whether an AWS config or credentials file exists, and which profiles it names
- whether a `credential_process` entry is configured (short-lived, auto-refreshing
  credentials — the recommended setup)
- whether this host mounts an MCP server that vends credentials directly, which
  some managed and enterprise editions install

Nothing in that section reads a secret value, and it never fails the overall
`doctor` run — a missing AWS setup is not a Kiro Crew fault.

## Adjusting the rules

**Settings → Security** lets you disable individual built-in rules, disable them
all, and add your own patterns. When you add your own rule, fill in the **note**
field: that note is what the agent is shown when your rule fires, instead of a
raw regex it has to guess the intent of.

Two limits are deliberate:

- The agent cannot edit any of this. The files live on the sensitive-path floor,
  which is what stops a prompt-injected agent from rewriting its own ceiling.
- On a host with an enterprise governance policy, the credential, sensitive-file,
  self-protection, git-publish, reverse-shell and pipe-to-shell categories are
  **pinned** and cannot be disabled — including by "disable all". Your
  organization's policy composes with your settings on a tightest-wins basis.

To see the ceiling on such a host:

```bash
kirocrew policy show
kirocrew policy explain <scope> <item>
```

These are CLI-only on purpose: the governed agent does not get a tool for
enumerating its own ceiling.

## Related

- [configuration.md](configuration.md) — sandbox modes and config reference
- [troubleshooting.md](troubleshooting.md) — general problems and fixes
