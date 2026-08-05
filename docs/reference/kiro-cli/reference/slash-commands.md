# Slash Commands

Source: https://kiro.dev/docs/cli/reference/slash-commands/

Available in interactive chat mode only.

## Commands

| Command | Description |
|---------|-------------|
| `/help` | Switch to Help Agent or show help text (`--legacy` for classic) |
| `/quit` | Exit chat (aliases: `/exit`, `/q`) |
| `/clear` | Clear conversation display |
| `/context show` | Display context rules and matched files |
| `/context add <pattern>` | Add context rules (files or globs) |
| `/context remove <pattern>` | Remove rules |
| `/model` | Interactive model picker or `/model <name>` |
| `/model set-current-as-default` | Persist current model for future sessions |
| `/agent list` | List available agents |
| `/agent create <name>` | Create new agent |
| `/agent edit [name]` | Edit agent config (supports `--path`) |
| `/agent generate` | AI-generated agent config |
| `/agent swap` | Switch agent at runtime |
| `/agent set-default <name>` | Set default agent |
| `/chat resume` | Interactive session picker |
| `/chat save <path>` | Save session to file |
| `/chat load <path>` | Load session from file |
| `/chat save-via-script <path>` | Save via custom script (JSON via stdin) |
| `/chat load-via-script <path>` | Load via custom script (JSON to stdout) |
| `/editor` | Open `$EDITOR` for multi-line prompt |
| `/reply` | Open editor with last assistant message quoted |
| `/compact` | Summarize conversation to free context space |
| `/paste` | Paste image from clipboard |
| `/tools` | View tools, token counts, permissions |
| `/tools trust <name>` | Trust tool for session |
| `/tools untrust <name>` | Revert to per-request confirmation |
| `/tools trust-all` | Trust all tools |
| `/tools reset` | Reset all to defaults |
| `/tools schema` | Show input schema for all tools |
| `/prompts list` | List available prompts |
| `/prompts get <name>` | Get prompt (shortcut: `@<name>`) |
| `/prompts create <name>` | Create local prompt |
| `/prompts edit <name>` | Edit local prompt |
| `/hooks` | View active context hooks |
| `/usage` | Show billing and credits |
| `/mcp` | Show loaded MCP servers |
| `/code init` | Initialize LSP code intelligence (`-f` to force) |
| `/code overview` | Workspace overview |
| `/code status` | LSP server status |
| `/code logs` | View LSP logs |
| `/experiment` | Toggle experimental features |
| `/tangent` | Create conversation checkpoint (also `Ctrl+T`) |
| `/todos` | View/manage to-do list |
| `/issue` | Report bug or feature request |
| `/logdump` | Create diagnostic log bundle |
| `/changelog` | View CLI changelog |

## Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+C` | Cancel current input |
| `Ctrl+J` | Insert newline (multi-line) |
| `Ctrl+S` | Fuzzy search commands and context files |
| `Ctrl+T` | Toggle tangent mode |
| `Up/Down` | Navigate command history |
