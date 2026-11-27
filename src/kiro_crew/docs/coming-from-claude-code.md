# Coming from Claude Code

<!--
Keeping this page current:
- One row per piece in "The pieces, in concrete terms". To sharpen a difference,
  edit that row; to add one, add a row. Keep each cell to one or two lines and
  link the piece's own page for detail instead of restating it.
- Claims about Claude Code itself live only in "About Claude Code" and carry the
  month they were checked. Do not add Claude Code claims anywhere else.
-->

Kiro Crew does not replace Claude Code. Crew is a program that runs around an
agent, and Claude Code can be that agent: pick it as the harness and Crew drives
it for every chat. What Crew adds is the part a terminal session does not keep
once you close it.

## The short answer

If you are a heavy Claude Code user, you may already do much of this with your
own scripts, hooks, notes files and habits. The piece Crew gives you is a
**gateway**: one process on your machine (`kirocrew gateway`, dashboard at
`http://localhost:5476`) that keeps running when no terminal is open. It holds
your chats, memory, schedules and running work on disk, wakes sessions on its
own, and lets you reach all of it from the dashboard, your phone or a chat app.
The habits you rebuild by hand in each new terminal session become things the
gateway keeps for you.

## The pieces, in concrete terms

| Piece | What it is | What it keeps or does for you | Read more |
|---|---|---|---|
| Gateway | The long-running Kiro Crew process. It runs every chat, bot and schedule. | Keeps working with no terminal open; the desktop app or `kirocrew service install` can run it for you. | [Running in the background](getting-started.md#running-in-the-background) |
| Chat | One conversation with an agent, listed in the dashboard sidebar. | Stays in the sidebar to come back to and can sit in a folder tied to a project. With the dashboard MCPs added to its agent, it can also open and drive other chats. | [Dashboard](dashboard.md), [Session control](session-control.md) |
| Memory | Notes Crew writes about you and your projects: preferences, project facts and corrections you gave it. | Adds them to the context of new chats, so you do not repeat yourself. | [Memory](memory-and-learning.md) |
| Bot | A named assistant you keep: an agent template, a workspace folder, its own memory store and a default model. | Opening it reopens the same pinned thread with its history, even after a restart or a week away. It learns into its own store, can hold goals, and can answer in a Slack channel bound to it. | [Bots](crew-members.md) |
| Conductor | A chat on the `kirocrew-conductor` agent that you give one large goal. | Splits the goal into items, starts one worker chat per item, and checks each one's result against a completion condition it set before the work began. The chat's **Board** tab lists its workers. | [Work ledger](work-ledger.md), [Conductor mode](agents.md#conductor-mode-in-the-dashboard) |
| Work ledger | The record shared by a conductor and its workers. | Each worker writes a status, a short summary and pointers (a branch, a PR number). The conductor reads that record instead of the worker's transcript. | [Work ledger](work-ledger.md) |
| Monitor loop | One chat that wakes itself on an interval, in the same conversation, until an exit condition is met. | "Keep an eye on this PR and tell me when it is review-ready": it checks, and speaks up only when there is something to say. | [Monitor loops](monitor-loops.md) |
| Session ledger | A small record on disk that a long-running session keeps: its goal, phase, next step and the approaches it already ruled out. | Lets the session pick up where it was after its early turns are compacted away or the gateway restarts. | [Session ledger](session-ledger.md) |
| Cron job | A prompt, or a script that needs no model, on a schedule. | "Every weekday at 9am, check my pipelines." Set it in words, on the **Schedule** page, or with `kirocrew cron add`. | [Cron jobs](cron-and-scheduling.md) |
| Channels | Slack, Telegram, Discord and other chat apps wired to the gateway. | Talk to your chats and bots from your phone, with no dashboard open. | [Chat channels](index.md#chat-channels) |
| Remote Crew | Another machine running its own gateway, such as a dev box. | Switch to it from the dashboard header, start chats that run there, and search every machine's history from one box. | [Remote Crew](remote-crew.md) |

## Using Claude Code inside Crew

- **Make it the harness.** Choose **Claude Code** under
  **Settings → Agent Harness**, or run
  `kirocrew config set agent.acp_backend claude`. Crew runs it through the
  public `claude-agent-acp` adapter ([ACP Backend](configuration.md#acp-backend)).
- **Crew still adds its part.** On Claude Code, Crew still adds your memory,
  learned corrections and its dashboard reply rules
  ([Why it behaves differently](coming-from-kiro-cli.md#why-it-behaves-differently)).
- **Bring in past sessions.** Crew does not import Claude Code transcripts, but an
  agent can read them under `~/.claude/projects/` and open one summary chat per
  session in a folder per project
  ([Bring in past Claude Code sessions](use-cases.md#bring-in-past-claude-code-sessions)).
  Chats you run on the Claude Code harness inside Crew are stored there too.
- **Run `claude` itself.** A chat's **Terminal** tab is a real shell in the chat's
  folder, so the `claude` command runs there as in your own terminal
  ([Terminal](dashboard.md#terminal)).

## About Claude Code

Checked November 2026. Claude Code is Anthropic's coding agent for the terminal
and IDE. It has its own sub-agents, hooks and `CLAUDE.md` memory files, and keeps
one transcript folder per working directory under `~/.claude/projects/`. It
changes quickly, so for what it does today read
[Anthropic's Claude Code docs](https://code.claude.com/docs/en/overview).
If your setup already covers a row in the table above, the difference is where
it lives: in Crew it runs in the gateway, not in a terminal you have to keep open.
