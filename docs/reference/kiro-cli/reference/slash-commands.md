# Slash Commands

Source: https://kiro.dev/docs/reference/slash-commands/ (fetched 2026-09-06;
upstream retired `/docs/cli/reference/slash-commands/`)

Available in interactive chat mode only. `scripts/docs_lint.py` hand-excepts this
directory from the per-directory-index rule, so moving or renaming the page means
editing that exception in the same change.

## Commands

| Command | Description |
|---------|-------------|
| `/help` | Switch to Help Agent or show help text (`--legacy` for classic) |
| `/guide` | Switch to the Guide agent for documentation-grounded help; terminal UI only |
| `/quit` | Exit chat (aliases: `/exit`, `/q`) |
| `/clear` | Clear conversation display |
| `/context show` | Display context rules and matched files |
| `/context add <pattern>` | Add context rules (files or globs) |
| `/context remove <pattern>` | Remove rules |
| `/model` | Interactive model picker or `/model <name>` |
| `/model set-current-as-default` | Persist current model for future sessions |
| `/effort [level]` | Set reasoning effort: `low`, `medium`, `high`, `xhigh`, `max`; persists, and per-model defaults live under `chat.modelDefaults` |
| `/agent list` | List available agents |
| `/agent create <name>` | Create new agent |
| `/agent edit [name]` | Edit agent config (supports `--path`) |
| `/agent generate` | Alias of `/agent create` |
| `/agent swap` | Switch agent at runtime |
| `/agent set-default <name>` | Set default agent |
| `/upgrade-agent [diagnostics]` | Migrate older agent configs to v3, backing originals up to `.json.bak` |
| `/spawn <task>` | Start a parallel long-running session; `--name` labels it, and a task description is required |
| `/plan` | Switch to the Plan agent, optionally with an immediate prompt |
| `/spec` | List specs, enter spec mode, run spec tasks, analyze requirements |
| `/goal <objective>` | Run an autonomous loop that verifies completion; `--max <n>` iterations (default 5), `/goal clear` cancels |
| `/chat resume` | Interactive session picker |
| `/chat save <path>` | Save session to file (also `/save`) |
| `/chat load <path>` | Load session from file (also `/load`) |
| `/chat save-via-script <path>` | Save via custom script (JSON via stdin) |
| `/chat load-via-script <path>` | Load via custom script (JSON to stdout) |
| `/sessions [clean]` | V3 session dashboard; `clean` previews and `clean --yes` deletes empty local sessions |
| `/session-id` | Print the current session ID |
| `/rewind [n]` | Fork the conversation at an earlier turn, leaving the original intact |
| `/checkpoint` | Manage workspace checkpoints (`init`, `list`, restore) |
| `/editor` | Open `$EDITOR` for multi-line prompt |
| `/reply` | Open editor with last assistant message quoted |
| `/compact` | Summarize conversation to free context space |
| `/paste` | Paste image from clipboard |
| `/copy` | Copy the last assistant response to the clipboard; skipped over 100KB |
| `/transcript` | Open the transcript in a pager, or save it to a file |
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
| `/config [category]` | Review the agents, MCP servers, Powers, steering, skills and hooks of a V3 session |
| `/powers` | List installed Powers |
| `/knowledge` | Manage the knowledge base for semantic search |
| `/usage` | Show billing and credits |
| `/stats [n]` | Request IDs, timings and token counts for recent turns; `/stats save <file>` exports. Hidden from tab-completion |
| `/mcp` | Show loaded MCP servers and registry status |
| `/mcp auth` / `/mcp cancel-auth` / `/mcp logout` | Drive a server's OAuth flow |
| `/code init` | Initialize LSP code intelligence (`-f` to force) |
| `/code overview` | Workspace overview |
| `/code status` | LSP server status |
| `/code logs` | View LSP logs |
| `/settings [theme\|display\|history]` | Interactive settings menu |
| `/theme` | Override prompt and response colors |
| `/title [text]` | Set, clear or show the terminal window title |
| `/experiment` | Toggle experimental features |
| `/tangent` | Create conversation checkpoint (also `Ctrl+T`) |
| `/todos` | View/manage to-do list |
| `/issue` | Report bug or feature request |
| `/logdump` | Create diagnostic log bundle |
| `/changelog` | View CLI changelog |

Every skill in `.kiro/skills/` or `~/.kiro/skills/` is also a slash command under
its own name.

## Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+C` | Cancel current input, or exit the session |
| `Ctrl+D` | Exit session; in the monitor, move between subagents with `Ctrl+D`/`Ctrl+U` |
| `Ctrl+G` | Open the subagent execution monitor |
| `Ctrl+J` | Insert newline (all terminals including tmux) |
| `Ctrl+O` | Expand collapsed shell output |
| `Ctrl+R` | Reverse incremental history search |
| `Ctrl+S` | Fuzzy search commands and context files; Tab selects several |
| `Ctrl+T` | Toggle tangent mode |
| `Alt+Enter` | Insert newline (Terminal.app, Ghostty) |
| `Alt+Backspace` | Delete previous word |
| `Shift+Enter` | Insert newline (iTerm2, Ghostty, Kitty, Warp, Zed) |
| `Shift+Tab` | Enter plan mode, or return from Plan and Guide |
| `Up/Down` | Navigate prompt history (per-session by default) |
| `Tab` | Drill into approval options, autocomplete file references |
| `Esc` | Close panels, cancel execution, clear the prompt queue |
