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

## Reading items from 1Password

The list also has a **Password managers** group with
one row, **1Password**. Connecting it lets a `secret://NAME` reference read an
item from your 1Password account. Kiro Crew only reads: it never creates, changes
or deletes anything in 1Password, and nothing from 1Password is copied into the
local vault.

Kiro Crew talks to 1Password through 1Password's own Python SDK, the optional
`1password` extra. The desktop app ships it. From a source checkout, pass
`--1password` to the installer. On any other install, the 1Password pane shows the
command that adds it to the gateway's Python, the same way a missing voice
package is shown. Where 1Password publishes no build of the SDK (musl Linux,
glibc older than 2.32, Windows on ARM, Python 3.14 or newer), the pane says
1Password cannot be connected on this system, and your own items work as before.

There are two ways to connect.

- **With the 1Password app** (the default). The 1Password app must run on the
  same computer as the gateway, signed in, with **Settings > Developer >
  Integrate with other apps** turned on. Type your account name, as shown at the
  top of the 1Password sidebar, then click **Connect with 1Password app** and
  click **Approve** in 1Password. Kiro Crew stores only the account name. The app
  approves Kiro Crew for ten idle minutes, and until you lock it; after that the
  row says **Needs approval** and offers **Approve again**. A reference that
  needs 1Password while the approval has run out asks the app again. So jobs
  that run while you are away need the token way below: while connected
  through the app, **Use a token instead** next to that line switches to it.
- **With a token.** Click **Use a token instead**, or, when the 1Password app is
  not on this computer, the pane opens on these steps. **Open 1Password** takes
  you to where you make a
  [service account](https://developer.1password.com/docs/service-accounts/get-started/)
  token; give it read access to the vaults you want, then paste the token and
  **Connect**. The token is stored in the local vault, used only inside the
  gateway when it asks 1Password, and never shown again, logged or given to the
  agent. A service account cannot read your built-in Personal or Private vault.

If an update leaves the gateway without the SDK, the connection is kept and
shows as unavailable, and references through it fail closed. Add the package
again and it works with the connection you already made.

Items from every vault the connection can read then appear under
**1Password · ACCOUNT**, beside your own. A
login lists its username and websites; nothing else. These items cannot be
edited or deleted here: change them in 1Password. Supported item kinds are
Login, Password, API Credential and Secure Note.

A reference looks in the local vault first. Only a name the local vault does
not hold is read from 1Password, so a local item wins over a 1Password item of
the same name; such a 1Password row says **Not used: the name is taken**. So
does a 1Password item named like an entry Kiro Crew keeps for itself (the
WakaTime sign-in tokens, a Connections OAuth entry): those names are only ever
read from the local vault. Two 1Password items with one name show as one row
that says **Not used: more than one item has this name**; give each its own
name in 1Password. When 1Password cannot be reached, does not answer in
time, is limiting requests, refuses the token, is not approved, or a name
matches more than one 1Password item, the MCP server does not start, and its error says why. A
reference never falls back to another item. Your own items keep working
whatever state 1Password is in.

**Disconnect** forgets the token. Nothing in 1Password changes.

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

Migration is deliberately narrow. Only the Jira credentials are moved today —
the global `JIRA_API_TOKEN` and the per-host `JIRA_TOKEN_<HEX>` entries — because
those are the only `.env` consumers in the migration set that resolve a
`secret://` reference. Every other credential in `.env` (Slack, Discord,
kiro-cli, and the rest) still reads the literal value, so rewriting its line
would hand the reader the reference string instead of the token and break
authentication. Those keys migrate as their readers become vault-aware.

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
