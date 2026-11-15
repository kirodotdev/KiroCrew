# A Bot's chat carries the tools of an app its owner ticked that Bot for

Decided by: PENDING. Proposal only, not a decision: a maintainer (@iamwhatever, who decided the 2026-11-04 entry, and @bolichen97 or @buluoray, who decided the 2026-11-02 entry) restates it on the pull request that adds this entry, and this line then names them and links that comment.
Date: 2026-11-15
Supersedes: 2026-11-02-a-crewmates-worker-carries-its-template-policy.md (in part: "the crewmate's chat (the conductor) keeps running the stock conductor spec and takes nothing from the file", narrowed for app tools the owner granted, behind a preview)
Supersedes: 2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md (in part: "a mate's [conductor] mounts only Crew's own MCP servers", for one case only: an app's server the owner ticked that Bot for joins that Bot's own chat, behind a preview; the Ultra Mode conductor, skills, and every other new server stay as that entry says)

## Decision

A Bot's chat still takes nothing from the Bot's own file, and the shared conductor spec is still written from source; behind the default-off `bot_chat_app_tools` feature preview, an app's MCP server joins one Bot's chat, built per session as the Bot's worker spec is, only while the app is enabled, offers that server in `contributes.botChatServers`, holds the owner-approved `tools` Bot scope (`permissions.bots`), and the owner ticked that Bot on the app's page; unticking the Bot, or disabling, updating or uninstalling the app so the server no longer qualifies, starts that chat's next turn on a fresh conversation without it.

Everything else in the 2026-11-02 entry, as amended by the 2026-11-06 and 2026-11-13 entries, stands. Everything else in the 2026-11-04 entry stands: a mate's conductor still mounts no other non-Crew server and loads only the skills it names, the Ultra Mode conductor is unchanged, and any other new ability still goes to workers.

## Why

- An app that works beside the owner's Bots needs a Bot to act on the app's data from its own chat, with its own memory. Today only platform servers reach that chat; routing the work through the Bot's worker costs a second turn and puts the Bot's words in the worker.
- The owner already decides which Bots an app may reach and with which scopes (`permissions.bots`, ticked per Bot); this adds one scope to that same decision instead of a second switch.
- A Bot the owner did not tick, a scope the owner did not approve, and every Bot while the preview is off, run exactly as before.
- The 2026-11-04 entry keeps conductors to Crew's own servers because a conductor only analyzes and dispatches, so new abilities belong on workers. A Bot's own chat is not only that: it is where the owner talks to the Bot and where the Bot keeps its memory, and an app's tool here answers the owner in that chat (read the app's inbox, post to the app's board). Sending each such call through a worker adds a turn, splits one reply across two chats, and gives the worker the Bot's words without its memory. The exception stays narrow so the reason still holds everywhere else: only a server the owner ticked that one Bot for, only behind the preview, each call still under the Bot's own approvals, and the Ultra Mode conductor and every other conductor unchanged.

## Downsides and how each is bounded

- **An app's bug or a prompt-injected tool result lands in the Bot's own chat.** That chat is the thread the owner talks in, it usually runs at the Profile's Trust posture, it holds the Bot's memory, and it dispatches the Bot's workers. Bounds:
  - Nothing mounts unless every gate holds, read each time the chat starts its agent: the `bot_chat_app_tools` preview is on (off by default, always off on LTS), the app is installed, enabled and not denied execution trust, it offers the server in `contributes.botChatServers`, the owner approved its `tools` Bot scope (an update that adds the scope waits for that approval), and the owner ticked this Bot on the app's page (a fresh install reaches no Bot).
  - The mounted entry is rebuilt from its launch keys only, so the app's `autoApprove` and its agent's `allowedTools` never reach the Bot's chat, and every tool the owner switched off for that server stays off. Each call is approved or asked exactly as any other tool call in that chat, at the Bot's own posture.
  - Disabling, uninstalling or withdrawing trust from the app stops every Bot chat that runs its server at once, a running turn included, and no Bot chat that starts during the stop can mount it. A stop that cannot be confirmed is not reported as done: the Apps page names the Bots it could not stop and offers a retry. Unticking the Bot or switching the preview off starts that chat's next turn on a fresh conversation, so the old tool names leave its context too.
  - Not bounded: at a Trust posture a call can run without a prompt, and a result the model reads can steer that turn. The owner's tick and scope approval are the gate for that, which is why the exception is limited to coordination tools (below).
- **A slow or hung app server delays the owner's own reply, not only a worker's.** Bounds: the owner can stop the turn as any turn. An app's server is a local process, so under the 2026-11-08 entry a call that stays silent for 30 minutes is cancelled and handed back to the model as a timeout. That limit is long for a reply the owner is waiting on, so the rules below require every offered tool to return in seconds; a call that can take longer belongs on a worker.
- **More servers per Bot chat means more processes per Bot.** Bounds: a server runs only in the chats of the Bots the owner ticked, one process per offered server per chat, and only while every gate above holds. Losing a gate removes it from that chat (immediately on disable, uninstall or trust withdrawal; on the next turn for an untick or the preview), and it ends with the chat's agent process. A Bot nobody ticked, and every Bot while the preview is off, runs no app process.

## Use cases

Each uses the Team Mode app, which offers its `team` server (the one its `lead` agent declares) to the Bots the owner adds as leads. The app knows a lead by the session label of that Bot's own chat (`dashboard:<the chat's slot>`), so a call from any other session, a worker included, answers `not_a_lead`.

1. **Catching up on the team.** The owner asks the Bot "anything from the other leads I need to act on?". The Bot calls `team_inbox_read`, which returns its unread items, secretary brief first, and marks them read. The Bot answers in the same reply, sorting the items against the work it remembers. Through a worker: the worker is not the lead, so the call is refused; were it allowed, the worker would mark the items read, and the Bot, which never saw them, would find an empty inbox on its next wake.
2. **Answering an assignment.** Another lead @-ed the Bot about the release notes. The owner says "take it, and tell them it lands Friday". The Bot calls `team_assignments` to find the assignment's id, then `team_send` with `answers=<assignment_id>`, `response` take and that text; it returns `accepted` with the message's sequence number, and the Bot confirms to the owner. Through a worker: the reply is the Bot's commitment and must be posted as the Bot, and the worker would have to relay the owner's words and the assignment id across two chats to do what one call does here.
3. **Following up a meeting.** The owner asks "what did the release meeting decide, and what is on me?". The Bot calls `team_meeting_status` (the meeting's state, agenda outcomes and action items) and `team_todo_list` (the action items this Bot accepted), and replies with the outcome and its own to-dos. When the owner later says one is done, the Bot calls `team_todo_close`. Through a worker: the to-dos are the Bot's, and the worker would read them with none of the Bot's memory of why it accepted them, then pass a summary back for the Bot to repeat.

## When this exception does not apply

An app offers a server in `contributes.botChatServers` only when every tool on it passes all of these; a server with one tool that fails any of them is not offered there, and a pull request that offers one anyway needs a new superseding entry, not this one:

1. It reads or writes only the app's own coordination state in the app's own data: messages, inboxes, boards, assignments, meeting turns, to-dos.
2. It runs no code or shell, changes no file outside the app's data, uses no credential, spends nothing, and makes no write call to any service outside the gateway.
3. It returns in seconds. Nothing it does waits on a build, a long network job or a person.
4. Its result is a short status or a list, not work output (a document, a diff, a log).
5. It does not do the work it coordinates. When the owner asks for work (code, research, a run), the Bot still dispatches a worker; the app's tool only posts or reads about that work.

An app that needs tools outside these rules gives them to its own agents or to the Bot's worker, as the 2026-11-04 entry says.

## Evidence

- The pull request that adds this entry, and the maintainer comment that restates it (to be linked).
- https://github.com/kirodotdev/KiroCrew/pull/32983 -- adds `permissions.bots` and the per-app ticked Bots this entry builds on.
- https://github.com/kirodotdev/KiroCrew/pull/37813 -- the implementation pull request for `feat/bot-chat-app-tools-v2`, which depends on this entry and on #32983.
