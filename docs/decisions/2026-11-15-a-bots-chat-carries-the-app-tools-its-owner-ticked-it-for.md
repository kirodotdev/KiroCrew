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

## Evidence

- The pull request that adds this entry, and the maintainer comment that restates it (to be linked).
- https://github.com/kirodotdev/KiroCrew/pull/32983 -- adds `permissions.bots` and the per-app ticked Bots this entry builds on.
- https://github.com/kirodotdev/KiroCrew/pull/37813 -- the implementation pull request for `feat/bot-chat-app-tools-v2`, which depends on this entry and on #32983.
