# Session Management

Source: https://kiro.dev/docs/cli/chat/session-management/ (fetched 2026-09-06)

Auto-saves every conversation turn to a local SQLite database (`~/.kiro/`).
Sessions are per-directory and identified by a UUID. On exit the CLI prints the
session ID and the command to resume it.

## Managing sessions

### From command line

```bash
kiro-cli chat --resume              # resume most recent
kiro-cli chat --resume-id <ID>      # resume a specific session
kiro-cli chat --resume-picker       # interactive picker
kiro-cli chat --list-sessions       # list all
kiro-cli chat --delete-session <ID> # delete
kiro-cli chat --sessions            # open the session dashboard at launch
```

### From chat

```bash
/chat new             # save current session, start a fresh one in place
/chat resume          # interactive picker
/session-id           # print the current session ID
/chat save <path>     # save to file
/chat load <path>     # load from file (.json optional)
```

## Session dashboard

A V3-only surface, opened with `kiro-cli chat --sessions` or `/sessions` from a
V3 session. It lists local and cloud sessions from every workspace; `Enter`
resumes the highlighted one, and a local session from another workspace asks you
to confirm the directory change first. Typing filters on session title,
workspace name and path, session ID and tag (`#tag`); a query of three or more
characters also searches indexed titles and user prompts from local V2 and V3
transcripts. Assistant responses, tool output, and classic and cloud transcripts
are not indexed.

`Ctrl+D` stages a deletion, confirmed with `y`. The active session and one open
in another terminal cannot be deleted. Empty-session cleanup also runs
non-interactively and cannot be undone:

```bash
/sessions clean         # report which empty local sessions would be deleted
/sessions clean --yes   # delete them
```

## Cloud sessions

A session started with `--cloud` is stored in the account's cloud session store
rather than the local per-directory database, and appears alongside local
sessions in `--list-sessions` and `/sessions`. `--resume-id` detects a cloud
session ID on its own, so it resumes from any machine without `--cloud`.
`/chat save` and `/chat load` act on the local archive and are unavailable in a
cloud session.

## Custom storage via scripts

```bash
/chat save-via-script <script-path>   # script receives JSON via stdin
/chat load-via-script <script-path>   # script outputs JSON to stdout
```

Example — save to Git notes:

```bash
#!/bin/bash
COMMIT=$(git rev-parse HEAD)
TEMP=$(mktemp)
cat > "$TEMP"
git notes --ref=kiro/notes add -F "$TEMP" "$COMMIT" --force
rm "$TEMP"
echo "Saved to commit ${COMMIT:0:8}" >&2
```

## Limitations

- A session can be active in only one process at a time; the second attempt
  reports the holding PID. Fork it with `/chat save` then `/chat load` instead.
- Sessions are stored per-directory
- Auto-save goes to the database only, not to files
- Session IDs are UUIDs
- Local sessions do not sync across machines; use scripts, or a cloud session
