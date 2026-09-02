# Chat

Source: https://kiro.dev/docs/cli/chat/

## Starting a session

```bash
kiro-cli                    # default
kiro-cli --agent myagent    # with specific agent
```

## Multi-line input

Use `/editor` or `Ctrl+J` to insert newlines. `/reply` opens editor with last assistant message quoted.

## Conversation persistence

Sessions are per-directory. Resume with:

```bash
kiro-cli chat --resume          # most recent session
kiro-cli chat --resume-picker   # interactive picker
```

### Manual save/load

```bash
/chat save ./my-conversation.json    # -f to overwrite
/chat load ./my-conversation.json
```

Save/load operate independently of the directory where the conversation was created.
