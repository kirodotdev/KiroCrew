# Session Management

Source: https://kiro.dev/docs/cli/chat/session-management/ (fetched 2026-11-23)

Auto-saves every conversation turn to a local SQLite database (`~/.kiro/`).
Sessions are per-directory and identified by a UUID. On exit the CLI prints the
session ID and the command to resume it. Kiro keeps its home state and past
session data owner-only, so other accounts on the machine cannot read them.

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

In V3, `--resume` picks the newest session that matches the launch: a local launch
resumes the newest local session, a cloud launch the newest cloud one. From CLI
3.3.0 a local launch skips a newer session that holds no prompt or turn and opens
the newest one with a conversation, and opens an empty session only when no other
exists; `--no-interactive --resume` follows the same rule. In the terminal UI an
unknown `--resume-id` no longer starts a replacement conversation, and with cloud
sessions on, a full ID is checked against the remote store before it is reported
as unresolved.

### From chat

```bash
/chat new             # save current session, start a fresh one in place
/chat resume          # interactive picker
/session-id           # print the current session ID
/chat save <path>     # save a V3 session archive (.zip; 2.x saves JSON)
/chat load <path>     # load an archive
```

A declarative workflow step has its own backing session, but you manage it
through `/workflow`, not `/chat resume`, and it is not guaranteed to be
searchable, exportable or resumable through `/sessions`.

## Session dashboard

A V3-only surface, opened with `kiro-cli chat --sessions` or `/sessions` from a
V3 session. It lists local and cloud sessions; by default only those from the
current directory, and Kiro remembers that filter in
`chat.sessionDashboard.scope`. `Enter` resumes the highlighted one, and a local
session from another workspace asks you to confirm the directory change first.
`Tab` moves between the controls (Search, Filter, Sort, Group) and the results,
and `Ctrl+X` resets the filters. Empty sessions are hidden unless you turn on
"includes empty", and "main sessions" hides child sessions from Tangent, rewind
and subagents.

Typing filters on session title, workspace name and path, session ID and tag
(`#tag`); a query of three or more characters also searches indexed content from
local V2 and V3 transcripts. By default the index covers your prompts and the
agent's responses; Session search in `/settings` switches it to prompts only.
Tool output, and classic and cloud transcripts, are not indexed.

`kiro-cli chat --sessions` selects V3 when you have not chosen a harness. With a
non-V3 harness passed or saved in `chat.agentEngine`, Kiro reports the conflict;
run `kiro-cli chat --v3 --sessions` for that launch.

`Ctrl+D` stages a deletion, confirmed with `y`. The active session and one open
in another terminal cannot be deleted. Empty-session cleanup leaves out active,
locked, recently active, marked and derived sessions, scans again before it
deletes, and cannot be undone:

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
