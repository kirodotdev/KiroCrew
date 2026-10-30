# Coming from kiro-cli

Kiro Crew is a layer on top of kiro-cli, not a replacement for it. Crew starts
kiro-cli in ACP mode (`kiro-cli acp`) and drives it over the Agent Client
Protocol, so by default kiro-cli's engine, your login and your agent files do
the work. What Crew adds
is everything a single terminal session does not keep: chats that persist,
memory, schedules, messaging channels, and sessions that hand work to other
sessions.

## What carries over

| From kiro-cli | In Kiro Crew |
|---|---|
| Your login (`kiro-cli login`) | Used as is. On the default harness you can instead sign in to Kiro from **Settings → Agent Harness**, and Crew then holds that sign-in itself. |
| Agents in `~/.kiro/agents/` and a project's `.kiro/agents/` | Switch a chat to one with `/agent <name>`; agents in `~/.kiro/agents/` are also offered by the agent chip under the message box. A project agent needs the chat to have that project as its folder. A JSON agent runs on both Kiro harnesses; a markdown agent needs the default one ([Agents](agents.md#markdown-agents)). |
| MCP servers in `~/.kiro/settings/mcp.json` | Merged into the default `kirocrew` agent. A custom agent keeps its own `mcpServers`; on the Kiro CLI harness it also loads `~/.kiro/settings/mcp.json` unless it sets `"includeMcpJson": false`. Add more under **Customize → Connections** ([Connections](connections.md)). |
| Steering in `~/.kiro/steering/` and `<project>/.kiro/steering/` | Loaded by kiro-cli, as in the terminal. Project steering needs the chat to have that project as its folder (see below). |
| Skills in `~/.kiro/skills/` | Not read by the default agent, whose skills live in `~/.kiro/crew/skills/`. Add `~/.kiro/skills` under **Settings → Skills → Extra skill paths** to use them there. A custom agent on the Kiro CLI harness still inherits `~/.kiro/skills`, as in the terminal ([Skills](skills.md)). |
| Slash commands | `/agent <name>`, `/compact` and `/clear` work in the chat box on both Kiro harnesses, as do Crew's own `/goal`, `/prompts`, `/side` and `/workflow`. kiro-cli's other commands (`/context`, `/mcp`, `/model`, `/tools`, `/usage` and the rest) run only on the **Kiro CLI** harness; the default harness answers that the command doesn't work there. Terminal-only ones such as `/quit`, `/chat`, `/paste`, `/editor` and `/tangent` are refused everywhere. |

## What Crew adds

- **Chats that persist.** Leave one and come back from the sidebar, file it in a
  folder, or let one chat open and drive others ([Dashboard](dashboard.md),
  [Session control](session-control.md)).
- **Memory.** Preferences, project facts and your corrections carry across
  chats ([Memory](memory-and-learning.md)).
- **Schedules.** "Every weekday at 9am, check my pipelines"
  ([Cron jobs](cron-and-scheduling.md)).
- **Channels.** Talk to it from Slack, Telegram and other messaging apps,
  including on your phone ([Chat channels](index.md#chat-channels)).
- **Sub-agents and conductors.** Fan work out to background workers, or have one
  chat split a goal across several ([Subagents](subagents.md),
  [Work ledger](work-ledger.md)).

## Why it behaves differently

A new Crew chat runs Crew's own agent, `kirocrew`, not kiro-cli's
`kiro_default`. It has a different system prompt, and Crew adds context to it:
your memory, learned corrections, the skill catalog, and rules for how replies
render in the dashboard. That is why the same request can get a different
answer, or a different approach, than it would in `kiro-cli chat`.

To work with a CLI-style agent instead:

- **Your own agent.** Pick any agent from `~/.kiro/agents/` with the agent chip
  under the message box, or type `/agent <name>`. A JSON agent works on both
  Kiro harnesses.
- **kiro-cli's built-in `kiro_default`.** `/agent kiro_default` works when the
  harness is **Kiro CLI**: choose it under **Settings → Agent Harness**, or run
  `kirocrew config set agent.acp_backend kiro`. The default harness, KAS
  (kiro-cli's v3 engine), does not offer kiro-cli's built-in agents, so use your
  own agent there.
- **Make it the default for new chats.** Open **Customize → Custom agents** and
  pick it in **New sessions use**. This writes `agent.default_agent`. The picker
  offers agents in `~/.kiro/agents/`, not a project's `.kiro/agents/` and not
  kiro-cli's built-ins.

On any agent, Crew still adds memory, learned corrections and the dashboard
reply rules. A custom agent in `~/.kiro/agents/` can set
`"includeCrewContext": false` to drop Crew's block of dashboard rules and its
dashboard-tool reminders; some reply-format guidance still comes with each turn.
To leave memory out of one chat, pick
**Temporary** in the memory mode button above the message box before the first
message ([Memory modes](memory-and-learning.md#memory-modes)).

## Running kiro-cli inside Crew

Open a **Terminal** tab in a chat's side panel. It is a real shell in the chat's
working directory, so `kiro-cli chat` runs there exactly as in your own
terminal. Highlight output and choose **Send to chat** to hand it to the Crew
chat ([Terminal](dashboard.md#terminal)).

## Sessions

A kiro-cli session you ran in a terminal is not a Crew chat, but you can carry
it over. Open **Older Sessions**, the footer at the bottom of the Sessions
list, and choose its **Kiro CLI** tab: it lists the titled sessions kiro-cli
saved on this machine, newest first. Click one to read it,
then choose **Continue in Crew**. Crew opens a new chat holding a copy of the
session, titled "(from kiro-cli)" and kept in a **from kiro-cli** folder in the
Sessions list, with the session's folder as its project when Crew can use that
folder. The terminal session itself is left untouched, so you can still resume
it in kiro-cli, and continuing it twice makes two separate chats.

By default the new chat continues from the session's transcript, the
conversation you can read, without the session state kiro-cli saved for the
model. The chat opens as usual, its title ends in "transcript only", and the
preview says before you click which it will be on your machine.

The chat resumes the full kiro-cli session only when all of these hold:

- Crew runs the **Kiro CLI** harness (**Settings → Agent Harness**), and
- MCP Tool Search is off, and
- the kiro-cli installed on this machine still runs that harness on its own
  engine. A kiro-cli that offers its v3 engine runs Crew's chats on Kiro Agent
  even when Kiro CLI is chosen, and then no setting gives a continued chat the
  saved state; the preview says so.

```bash
kirocrew config set agent.acp_backend kiro
kirocrew config set agent.tool_search false
```

Tool Search is on by default, and while it is on every resumed dashboard chat
on kiro-cli starts a fresh session from its transcript, because a loaded
session would come back without the tools Tool Search had loaded
([Configuration](configuration.md)). Turning it off sends every MCP tool
definition with each turn again.

## Giving Crew a task the way you gave the CLI one

- **Point it at your repo.** kiro-cli works in the directory you start it from.
  A Crew chat works in its project folder: set it with the project button in the
  message box, or start the chat in a sidebar folder that has a linked project.
  Without one, it works in Crew's own workspace, not your repo.
- **Choose how much it may do unasked.** kiro-cli asks before running tools
  unless you trust them. In Crew, the approval-mode picker in the message box
  sets this: Normal, Reads and Trust apply to that chat, while YOLO
  auto-approves every session on this Kiro Crew.
- **Pick the agent and model first.** The agent chip and the model button set
  both per chat, so a chat stays on what you chose.
- **One task per chat.** A chat keeps its history, so a fresh task gets a fresh
  chat; the old one stays in the sidebar to come back to.
