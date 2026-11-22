# Secrets vault

The secrets vault stores credentials encrypted on disk, where the agent cannot
read them. A secret you put in the vault stays reachable to the code that needs
it and stays refused to every file read and shell command the agent runs — so a
prompt injection that talks the agent into printing your tokens has nothing to
print.

## Where it lives, and why the agent cannot read it

Each entry is encrypted separately with AES-256-GCM under a key file that only
your account can read. Both files sit in a `.vault` directory inside your Kiro
Crew data home, alongside `config.json`.

`.vault` is registered as a sensitive path, which is a check that runs before the
verb rather than per tool. That covers a file read, a `cat`, and a scripted
`python -c "open(...)"` alike — the refusal does not depend on the agent choosing
a route the policy anticipated. The protection is enforced by Kiro Crew, not by
the operating system: another program running as you can still open the files.

## Seeing which code read a secret

Turn on `dashboard.audit_secret_reads` (off by default) to have the gateway write
a security log row whenever it reads a vault secret, including the reads that
resolve an MCP server's `secret://` references. The row names the secret,
the module that read it and the function that asked, and never holds the value.
A read handed to a worker thread as a bare method has no calling code on that
thread, so its row names `unknown`. A name longer than 128 characters is
clipped in the row and marked with the start of its digest.
Repeat reads of one secret by the same code share one row per 10 minutes, which
counts them, so the log does not grow with every turn. A process with no live
config, such as the MCP gateway process, rereads the setting at most every 30
seconds, so a change reaches it within that time. See
[Configuration](configuration.md#dashboard).

## Adding a secret

Use **Settings → Secrets** in the dashboard. The left list holds every entry the vault
knows, grouped into the credentials Kiro Crew uses by name and your
own; picking one opens it on the right. **Add secret** asks what you are
storing first: a **Secret** (one value, an API key or token), a **Login** (a
website sign-in: username, password, the sites it belongs to) or a **Note**. The pane shows the `secret://` references
to paste, and lets you replace or delete the entry. **Used by** lists what
reads the entry now: MCP servers (their `env`, or an owner-added server's
sign-in), `.env` credential keys, scheduled jobs' approved grants, the
`agent.deepseek_env`, `agent.strands_env` and `tunnel.relay_lease_credential`
settings, Connections cards' sign-ins, the Bitbucket Server, Linear, Jev and
OpenTelemetry integrations, `kirocrew secrets authorize` approvals and Sprites
profiles. **No use found** means none of those reads it, which is how you spot
a likely stale entry; it is not proof, since a feature not on that list can
still read an entry by name. It never shows a stored
value back to you: a login lists its username and websites, nothing else, so a
screenshot of the panel leaks nothing. On a phone the list is the page and a
row opens its pane.

If a value fails to save, the form keeps what you typed rather than clearing it,
so you do not have to fetch the credential again.

## Signing in to a website from the vault

When the agent's browser asks you to sign in, the
sign-in card lists your saved logins for that site, one button each, labelled
by the account's username. The first three show; **More** shows the rest. Click
one and Kiro Crew types its username and password into the page for you;
the agent never sees them and only learns whether the sign-in went through.
Nothing is filled until you click.

A login counts as "for that site" when it shares the site's base domain: a
login saved for `appleid.apple.com` is offered on `developer.apple.com`, but one
for `alice.github.io` is not offered on `bob.github.io`. For `localhost` or an
IP address the host and port must match exactly. A login saved for an `https`
address is never offered on an `http` page.

To use a different account, open **Other account** and type it. When no saved
login matches, the card asks you to type, as before. Either way, tick **Save to
Secrets** if you want to keep what you typed. It is off by default. After the sign-in is
typed into the page, the login is saved as a new entry named after the site
(`github.com`, or `github.com (2)` if that name is taken); an existing entry is
never replaced.

## Moving credentials out of `.env`

`kirocrew secrets import` migrates plaintext credentials from `.env` into the
vault and rewrites each line to a `secret://KEY` reference — the file keeps a
pointer where the token used to be, and the reader resolves it from the vault.

It is a **dry run by default**: the bare command reports what it would move and
writes nothing. Add `--apply` to perform the migration.

```bash
kirocrew secrets import           # report only
kirocrew secrets import --apply   # store the secrets and rewrite .env
```

Migration is deliberately narrow. It moves only credentials whose every reader
resolves a `secret://` reference: the Jira credentials (the global
`JIRA_API_TOKEN` and the per-host `JIRA_TOKEN_<HEX>` entries) and the channel
secrets (the Slack app and bot tokens, and the Telegram, Discord, Webex, Teams,
WeCom, Weixin and Feishu secrets). Every other credential in `.env` (kiro-cli's
own key, the Azure DevOps PAT, and the rest) is read by a child process from its
inherited environment, so rewriting its line would hand the reader the
reference string instead of the token and break authentication. Those keys
migrate as their readers become vault-aware.

Running the import again is safe: a key whose value is already a `secret://`
reference is reported as such and skipped.

## Recovering from a failed import

An import computes its rewrite from a snapshot taken at the start. If `.env`
changes underneath it — another writer adding a token, a channel connected from
the dashboard mid-run — the rewrite is abandoned without writing, so a token
someone else just added is never overwritten.

If an apply fails partway and a rollback cannot delete the vault entries it
already wrote, the report names those keys. A leftover entry shadows a later
rotation of the same key, so delete the named entries under **Settings →
Secrets**.

## Related docs

- [Configuration](configuration.md): the config file, `.env`, and environment variables
- [Blocked commands](blocked-commands.md): why a read was refused and what to do instead
- [Snapshot and restore](snapshot-and-restore.md): what a snapshot excludes, and why
