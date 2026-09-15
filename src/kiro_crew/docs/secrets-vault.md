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

## Adding a secret

Use **Settings → Secrets** in the dashboard. The panel lists the names it holds,
adds a new entry, and deletes one. It never shows a stored value back to you —
listing returns names only, so a screenshot of the panel leaks nothing.

If a value fails to save, the form keeps what you typed rather than clearing it,
so you do not have to fetch the credential again.

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
those are the only readers that resolve a `secret://` reference. Every other
credential in `.env` (Slack, Discord, kiro-cli, and the rest) still reads the
literal value, so rewriting its line would hand the reader the reference string
instead of the token and break authentication. Those keys migrate as their
readers become vault-aware.

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
