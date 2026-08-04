# Session Management

Source: https://kiro.dev/docs/cli/chat/session-management/

Auto-saves every conversation turn to local SQLite database (`~/.kiro/`). Sessions are per-directory.

## Managing sessions

### From command line

```bash
kiro-cli chat --resume              # resume most recent
kiro-cli chat --resume-picker       # interactive picker
kiro-cli chat --list-sessions       # list all
kiro-cli chat --delete-session <ID> # delete
```

### From chat

```bash
/chat resume          # interactive picker
/chat save <path>     # save to file
/chat load <path>     # load from file (.json optional)
```

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

- Sessions stored per-directory
- Auto-save to database only (not files)
- Session IDs are UUIDs
- No cloud sync (use scripts for custom storage)
- No session search by content
