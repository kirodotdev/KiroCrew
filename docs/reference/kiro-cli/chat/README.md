# Chat

Source: https://kiro.dev/docs/cli/chat/ (fetched 2026-09-06)

## Starting a session

```bash
kiro-cli                            # default, rich terminal UI
kiro-cli --agent myagent            # with a specific agent
kiro-cli --cloud --repo owner/repo  # managed cloud sandbox, optional repo bind
```

## Multi-line input

`Shift+Enter` in iTerm2, Ghostty, Kitty, Warp and Zed; `Ctrl+J` everywhere
including tmux; `Alt+Enter` in Terminal.app and Ghostty; `/editor` opens your
editor (vi by default). `/settings terminal` auto-configures `Shift+Enter` where
it does not work. `/reply` opens the editor with the last assistant message
quoted.

## Inline shell commands

Prefix with `!` to run a command without going through the model. Output streams
live, TTY commands such as `vim`, `ssh` and `top` get full terminal access, and
long output collapses to head plus tail with `Ctrl+O` to expand.

```bash
!npm run build
```

## Context

```bash
/context show                # breakdown with per-file token usage
/context add "src/**/*.ts"   # add by glob
/context remove src/app.js   # remove a rule
/context clear               # remove all rules
```

## Conversation persistence

Sessions are per-directory. Resume with:

```bash
kiro-cli chat --resume                 # most recent session
kiro-cli chat --resume-id <SESSION_ID> # a specific session
kiro-cli chat --resume-picker          # interactive picker
```

`/chat new` saves the current session and starts a fresh one in place, and takes
an optional initial prompt. `/chat resume` returns to a previous session.

### Manual save/load

```bash
/chat save ./my-conversation.json    # -f to overwrite
/chat load ./my-conversation.json
```

Save/load operate independently of the directory where the conversation was
created, and `~` is not accepted as the home directory. Loading replaces the
current conversation.
