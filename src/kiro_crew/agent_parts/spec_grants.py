"""The server grants each installed agent gets without an approval prompt.

The guide and mate tool grants, the conductors' and crewmates' dashboard and panel
grants, the work grants and the worker's exclusions. Each tuple's comment states the
rule a new grant must meet. A new grant tuple goes here.

Read through ``kiro_crew.agent``, which binds every name here; see
:mod:`kiro_crew.agent_parts`.
"""

from __future__ import annotations

#: The platform guide server (``kiro_crew.mcp_guide``), mounted on every crewmate.
_GUIDE_SERVER = "kirocrew-guide"


#: The guide tools that run without a prompt on every agent, through the same
#: ``allowedTools`` ceiling every grant passes (``_apply_allowed_tools_ceiling``),
#: so a governance ceiling that denies one still keeps it out. All of them either
#: read (packaged docs and location index, a setting's value, a crewmate's
#: capability rows, a diagnosis, a guide's or card's status) or OFFER: a guide
#: and a change card are inert until the user presses their button in the
#: dashboard, and nothing automated can press it. ``guide_cancel`` removes this
#: conversation's pointer and undoes nothing the user saved. A tool added to the
#: server is not in this tuple, so it asks first until it is reviewed here.
_GUIDE_AUTO_GRANTS = (
    f"@{_GUIDE_SERVER}/find_ui",
    f"@{_GUIDE_SERVER}/search_docs",
    f"@{_GUIDE_SERVER}/guide_list_actions",
    f"@{_GUIDE_SERVER}/guide_start",
    f"@{_GUIDE_SERVER}/guide_status",
    f"@{_GUIDE_SERVER}/guide_cancel",
    f"@{_GUIDE_SERVER}/list_change_kinds",
    f"@{_GUIDE_SERVER}/find_setting",
    f"@{_GUIDE_SERVER}/get_member_capabilities",
    f"@{_GUIDE_SERVER}/diagnose_settings",
    f"@{_GUIDE_SERVER}/propose_change",
    f"@{_GUIDE_SERVER}/get_change_status",
)


#: The crewmate's own-record server (``kiro_crew.mcp_mate``), mounted on every
#: crewmate like the guide.
_MATE_SERVER = "kirocrew-mate"


#: Member tools kiro-cli runs without a prompt. Reviewed as a set, and every one
#: passes the ``allowedTools`` ceiling every grant passes. ``update_self`` writes
#: the crewmate's own presentation (name, description, face) on the user's word:
#: the gateway admits only a turn the user sent in the crewmate's own chat, and
#: the change lands in that chat as one row the user can undo. The three ``set_*``
#: / ``request_*`` tools only OFFER a change card, inert until the user presses
#: it (with the crewmate's Dynamic capabilities off, ``request_capabilities``
#: offers the Preview opt-in card first). ``find_capabilities`` only reads the
#: worker's tools and the catalog.
#: ``save_notes`` writes only the calling crewmate's own briefing, the file it
#: has always kept for itself, admitted only in its own thread. A tool
#: added to the server is not in this tuple, so it asks first until it is
#: reviewed here.
_MATE_AUTO_GRANTS = (
    f"@{_MATE_SERVER}/update_self",
    f"@{_MATE_SERVER}/set_model",
    f"@{_MATE_SERVER}/set_approval_mode",
    f"@{_MATE_SERVER}/request_capabilities",
    f"@{_MATE_SERVER}/find_capabilities",
    f"@{_MATE_SERVER}/goal_read",
    f"@{_MATE_SERVER}/set_goal",
    f"@{_MATE_SERVER}/set_key_results",
    f"@{_MATE_SERVER}/set_check_ins",
    f"@{_MATE_SERVER}/offer_dashboard_page",
    f"@{_MATE_SERVER}/key_result_progress",
    f"@{_MATE_SERVER}/save_goal_report",
    f"@{_MATE_SERVER}/save_notes",
)


#: The dashboard verbs the conductor may call WITHOUT an approval prompt, named one
#: by one rather than as the whole ``@kirocrew-dashboard`` server.
#:
#: THE INVARIANT, so a later reader extends this by rule and not by taste. A
#: granted verb must satisfy BOTH halves:
#:
#: 1. It may CREATE something new or READ. It may never MUTATE user-visible
#:    workspace state that already exists and is not the conductor's own — a
#:    session's contents or liveness, or the arrangement the person made of their
#:    sessions and folders.
#: 2. Its worst case, called in a loop, must be BOUNDED BY THE SERVER — and
#:    bounded so the resource stays reachable by everyone else.
#:
#: The conductor ingests untrusted text by design — its charter's worked example is
#: "resolve this repo's open issues", and it holds ``web_fetch`` for exactly that —
#: so every granted verb is reachable by content it read, with no human in the loop
#: on a nudge-driven patrol cycle. Per-call approval was the only thing
#: rate-limiting a granted verb, and ``allowedTools`` has no argument or rate
#: matching to replace it, so the bound cannot live in this list: it has to live in
#: the endpoint. Half 2 is not a restatement of half 1 — an unbounded create is how
#: a create does damage without mutating anything.
#:
#: Half 1 names user-visible workspace state deliberately, rather than "any
#: pre-existing resource", because a create ALWAYS writes some shared bookkeeping —
#: the slot table, the folder index, the session-pulse counter below — and a literal
#: reading would forbid every create and decide nothing. What it protects is state
#: the person arranged and would have to reconstruct by hand. Creation is otherwise
#: recoverable clutter; mutation of what the user arranged is not.
#:
#: Both granted creation verbs earn half 2 from a server ceiling, and BOTH ceilings
#: were added by this change — neither verb was safe to auto-approve as the code
#: stood:
#:
#: * ``chat_folder_create`` had no bound at all, so a loop grew durable on-disk
#:   state without limit. Now ``MAX_CHAT_FOLDERS``, tested under the folder lock.
#: * ``session_create`` had a GLOBAL ceiling (``MAX_LIVE_SLOTS``) but no
#:   distribution: one caller could hold all 500, and every later create — the
#:   person opening a chat tab included — got the 429. A bounded resource that one
#:   caller can exhaust is not bounded from anybody else's point of view. Now
#:   ``MAX_SLOTS_PER_CREATOR`` bounds what a single caller holds, leaving 450 slots
#:   reachable no matter what the conductor does.
#:
#: Every verb this server exposes, against that rule:
#:
#: * ``chat_folder_tree`` — READ of the caller's visible tree. GRANTED.
#: * ``chat_folder_create`` — creates a NEW folder, and
#:   ``_refuse_tree_shaping_if_unverifiable`` refuses an unverifiable caller and
#:   keeps an app agent out of the person's own folders. Touches nothing that
#:   already existed, and bounded by ``MAX_CHAT_FOLDERS``. GRANTED.
#: * ``chat_folder_file_self`` — writes the CALLER'S OWN ``folder_id``, and only
#:   that: the target slot is the ``dashboard:<slot>`` the verified caller key
#:   names (``mcp_dashboard._own_chat_slot``; a linked channel/cron slot is
#:   refused because its binding can be rebound between read and write),
#:   there is no ``session`` argument, so ingested content cannot aim it at a
#:   peer. The one placement it can change
#:   is the conductor's own — the ``not the conductor's own`` clause of the
#:   invariant is exactly what admits it. This is what lets a conductor sit
#:   INSIDE the goal's folder beside its workers' subfolders instead of
#:   floating at the top level; the destination path is created on the way
#:   (mkdir -p, bounded by ``MAX_CHAT_FOLDERS`` like any create). GRANTED.
#: * ``session_create`` — creates a NEW session in the caller's workspace, bounded
#:   both globally (``MAX_LIVE_SLOTS``, 429 on breach) and per caller
#:   (``MAX_SLOTS_PER_CREATOR``), and visible in the sidebar. GRANTED.
#:   One known side effect, recorded because it is the closest thing to an
#:   exception here: ``create_session`` mints its slot with
#:   ``origin=SlotOrigin.USER`` (it is a first-class user-owned session, which is
#:   what keeps it correctly private), and ``get_or_create_slot`` increments the
#:   session-pulse counter on exactly that origin — so conductor-created sessions
#:   count toward the feedback survey's "10 genuine user chats" window. That is a
#:   conflation in the counter itself, not something this grant introduces:
#:   the counter uses the ownership tag as a proxy for "a person started a chat",
#:   and it miscounts for every caller of the session-control create verb. Not
#:   point-fixed here, because the correct fix is a fail-open/fail-closed
#:   decision about which call sites opt in, inside the session-pulse surface.
#:   Consequence if it drifts: a survey prompt appears earlier than the product
#:   intended. No workspace state is altered.
#: * ``session_read_message`` — read-only, and the verb the patrol loop actually
#:   needs on a cycle with nobody at the keyboard. GRANTED.
#: * ``session_summary`` — WITHHELD, though it is a read authorized exactly as
#:   ``session_read_message`` is and returns a digest of the same transcript: a
#:   verb is granted for a step, and no conductor step calls it yet. A skill that
#:   adopts it for the patrol cycle adds it here with that step as the reason.
#: * ``chat_folder_move_session`` — WITHHELD. It writes another session's
#:   ``folder_id``: the PATCH goes to ``/api/chat/slots/<target>/folder`` where the
#:   target is the session named in the ARGUMENTS, and the strictly-resolved
#:   caller key is only the authority header. ``mcp_dashboard`` calls it "the one
#:   tool here that writes to a session OTHER than the caller's". Auto-approving it
#:   would let ingested content silently refile or unfile any persistent
#:   same-workspace session, losing filing the user did by hand.
#: * ``chat_folder_move`` — WITHHELD. Reparents an existing folder tree, and no
#:   conductor step needs it.
#: * ``chat_folder_delete`` — WITHHELD. It passes the invariant: the dashboard
#:   removes only an empty folder the CALLER's own session created and the
#:   person has not touched since, and refuses an app or crew member outright.
#:   It is withheld for the ``session_summary`` reason: no conductor step calls
#:   it yet. A skill whose cleanup step adopts it adds it here with that step.
#: * ``chat_folder_update`` — WITHHELD. Renames or restyles an existing folder,
#:   which may be the person's, and no conductor step needs it.
#: * ``chat_tag_list`` / ``chat_tag_create`` / ``chat_tag_update`` — WITHHELD,
#:   not because any fails the invariant (a read, a create that dedups on name,
#:   and a metadata edit that loses no assignment) but because no conductor step
#:   needs them; a verb is granted for a step, not for being harmless.
#: * ``chat_tag_assign`` — WITHHELD. Writes another session's ``tags``: the PUT
#:   goes to ``/api/chat/slots/<target>/tags`` where the target is the session
#:   named in the ARGUMENTS — the same shape as ``chat_folder_move_session``.
#:   Ingested content could re-label any persistent same-workspace session.
#: * ``chat_session_pin`` — WITHHELD. Writes another session's ``pinned`` flag:
#:   the PATCH goes to ``/api/chat/slots/<target>/pin`` where the target is the
#:   session named in the ARGUMENTS, the same shape as ``chat_tag_assign``, and
#:   no conductor step needs it.
#: * ``chat_tag_column_list`` / ``chat_tag_column_create`` — WITHHELD. A read
#:   and an append that dedups on name and tag, so neither fails the invariant;
#:   withheld because no conductor step needs them, like the tag verbs.
#: * ``chat_tag_column_move`` — WITHHELD, on the invariant: it MUTATES the order
#:   of columns the person arranged, which is existing state that is not the
#:   caller's own, and no conductor step needs it.
#: * ``session_move_to_column`` — WITHHELD, on ``chat_tag_assign``'s reason: the
#:   drop retags the session named in the ARGUMENTS
#:   (``/api/chat/slots/<target>/drop``), so ingested content could move any
#:   persistent same-workspace session between columns. No conductor step needs it.
#: * ``session_rename`` — WITHHELD. Writes another session's title through
#:   ``/api/session-control/rename`` where the target is the session named in the
#:   ARGUMENTS, and the rename is final, so the automatic titler never corrects a
#:   bad one. Ingested content could relabel any session the conductor may reach.
#: * ``session_generate_title`` — WITHHELD. Spends a model call and replaces
#:   another session's sidebar title through ``/api/session-control/generate-title``
#:   where the target is the session named in the ARGUMENTS, and no conductor step
#:   needs it.
#: * ``session_send`` — WITHHELD. Runs text as another session's user-role turn
#:   under that target's own grants. The server-side gates bound WHICH target is
#:   reachable; nothing bounds WHAT is sent.
#: * ``session_broadcast`` — WITHHELD, on ``session_send``'s reason multiplied by
#:   the fleet: one call runs ingested text as a user-role turn in every session
#:   this agent created. Nothing about the fan-out narrows what is sent, so the
#:   verb inherits the withholding rather than earning its own argument.
#: * ``session_status`` — GRANTED (below). A pure READ, and a narrower one than
#:   ``session_read_message``, which is already granted: it returns the caller's own
#:   children with a liveness word each and no transcript content at all. The
#:   patrol cycle is exactly where it is needed — the cycle runs unattended, and its
#:   first question every round is which workers are still alive — so gating it
#:   would put an approval prompt in the loop with nobody at the keyboard, which is
#:   the cost this list exists to avoid.
#: * ``session_adopt`` — WITHHELD, on the invariant rather than on a judgement about
#:   how bad it would be. It MUTATES workspace state that already exists and is not
#:   the caller's own: where another session hangs in the tree, which is what the
#:   sidebar shows the person. A takeover moves that session's whole subtree with it,
#:   so one auto-approved call on an ingested-content cycle rearranges a part of the
#:   sidebar nobody asked to have rearranged. The verb exists for a person deciding to
#:   consolidate conductors, and that decision is exactly what an approval prompt
#:   records.
#: * ``session_release`` — WITHHELD, for the same reason and with one honest
#:   asymmetry: releasing ITSELF is the agent's own state and would pass the
#:   invariant, while releasing a session it holds is not. The tool is one verb, so
#:   it is judged on its wider reach; an agent that needs to get out from under a
#:   stopped conductor gets an approval prompt, which a person is present for by
#:   definition when they are the one consolidating.
#: * ``session_stop`` — WITHHELD. Ends another session's in-flight turn and
#:   DISCARDS its work (``stop_target``: "A stop cancels cooperatively", and the
#:   cancelled turn's work is gone either way — the retry de-duplication that
#:   keeps a re-sent stop from ALSO discarding the queue does not make the verb
#:   non-destructive).
#: * ``session_end_wait`` — WITHHELD. Discards nothing, but it moves another
#:   session's turn forward (the target's ``wait`` returns early), which is a
#:   change to state that is not the caller's own, and no conductor step needs it
#:   unattended.
#: * ``session_reload`` — WITHHELD. Tears down another session's agent process
#:   and relaunches it. The conversation survives, but a reload is still a
#:   process-level action on a session a person may be watching, and no
#:   conductor step needs it.
#: * ``session_retry`` — WITHHELD here, GRANTED to members (below). It sends no
#:   new text: it re-runs a failed last turn through Resume's path, and it refuses
#:   any turn that did not fail. But it still starts a turn in a session that is
#:   not the caller's own, and the conductor has no ownership fence to bound which
#:   session that is.
#: * ``session_continue`` — WITHHELD here AND from members. Unlike
#:   ``session_retry`` it does not refuse a turn that finished: it tells the
#:   target to carry on, so every call asks for an approval until an owner
#:   decides it belongs in the member set.
#:
#: Every withheld verb stays MOUNTED (``@kirocrew-dashboard`` is still in
#: ``tools``) — it just passes through ``hooks.on_tool_call`` like any ungranted
#: tool. The cost is an approval when a round files a session, seeds a child, or
#: stops one; all three happen right after a human approved the plan, while the
#: unattended patrol cycle needs none of them. A ``folder`` argument on
#: ``session_create`` would remove the filing call altogether.
_CONDUCTOR_DASHBOARD_GRANTS: tuple[str, ...] = (
    "@kirocrew-dashboard/chat_folder_tree",
    "@kirocrew-dashboard/chat_folder_create",
    "@kirocrew-dashboard/chat_folder_file_self",
    "@kirocrew-dashboard/session_create",
    "@kirocrew-dashboard/session_read_message",
    "@kirocrew-dashboard/session_status",
)


#: The dashboard verbs a CREW MEMBER's DM session may call without an approval
#: prompt. Superset of the conductor's: the write verbs (``session_send``,
#: ``session_stop``) join because a member's reach is SERVER-bounded in a way
#: the conductor's is not — ``authorize_target`` refuses a member caller on any
#: session it did not itself create (``created_by`` ownership, 403), so the
#: worst case of an auto-approved write is confined to worker sessions the
#: member opened, never the person's own conversations. The conductor has no
#: such ownership fence, which is why its list withholds the writes. Without
#: these two the dispatch loop this feature exists for (create → seed → patrol
#: → stop) stalls on an approval prompt at its second step with nobody at the
#: keyboard.
#: ``session_broadcast`` joins on exactly ``session_send``'s argument rather than a
#: new one, and the fan-out does not widen it: the verb's DEFAULT audience is read
#: from the same ``created_by`` field the ownership fence reads, and every delivery
#: re-runs that fence per target, so a member's broadcast reaches the worker
#: sessions it opened and nothing else — the same confinement, applied to each of
#: several targets instead of one. A member telling its whole fleet "the base moved"
#: is the ordinary case of the dispatch loop these grants exist for, and the
#: alternative is one approval prompt per worker on an unattended cycle.
#: ``session_retry`` joins under the same fence and is narrower than
#: ``session_send``. It runs ``authorize_target`` for the caller, so it reaches
#: only worker sessions the member opened. It sends no text: the resumed turn is
#: the one the worker already had, queued without the authenticated-human flag.
#: And it refuses a turn that finished or was stopped (``turn_not_failed``).
#: Restarting a worker whose turn timed out is the patrol step the verb exists
#: for, and that step runs with nobody at the keyboard.
_MEMBER_DASHBOARD_GRANTS: tuple[str, ...] = _CONDUCTOR_DASHBOARD_GRANTS + (
    "@kirocrew-dashboard/session_send",
    "@kirocrew-dashboard/session_broadcast",
    "@kirocrew-dashboard/session_stop",
    "@kirocrew-dashboard/session_retry",
)


#: The panel verbs a CREW MEMBER's DM session may call without an approval
#: prompt. BOTH of them, which is the whole surface ``kirocrew-panel`` exposes.
#:
#: This does not contradict the rule ``mcp_panel``'s own module doc states for
#: that server ("No ``autoApprove`` key ... this tool's input is derived from
#: text the agent read unattended"). The two paths differ in exactly the thing
#: that rule is about. An ``autoApprove`` key is resolved inside kiro-cli, emits
#: no permission request, and so skips ``hooks.on_tool_call`` -- the always-on
#: deny floor, the sensitive-path check and the governance ceiling. A grant named
#: here travels as ``allowedTools`` and is filtered by
#: ``kas_agents._ceiling_permitted`` through ``may_skip_gate_now``, which fails
#: closed, before any rule reaches the wire. So the ceiling the ``autoApprove``
#: key would have bypassed is the ceiling this grant crosses, and an operator who
#: governs either verb still governs it.
#:
#: ``panel_templates`` is a read and needs no further argument.
#:
#: ``panel_publish`` is a write, and it is granted on the invariant the dashboard
#: tuples above are judged by -- a granted verb may CREATE or READ, never MUTATE
#: something that already exists and is not the agent's OWN -- taking the same
#: asymmetry those tuples record for ``session_release``: the panel a call writes
#: is the CALLING CREW'S own, which is that agent's state. The server cannot be
#: pointed anywhere else. It takes no crew or session argument at all, resolves
#: the publishing crew strictly from the calling session, and refuses a subagent
#: outright rather than walking ``/proc`` ancestors to its parent's panel. The
#: worst case of an auto-approved call is therefore a member's own drawer showing
#: something its own unattended cycle put there, which is what the surface is for.
#:
#: Withholding ``panel_publish`` instead would cost the capability rather than
#: bound it: a panel exists to be refreshed once per cycle of long-running work
#: with nobody at the keyboard, so an approval prompt on the write verb stalls
#: exactly the unattended loop the drawer is watched during, and the operator's
#: real switch for that is ``agent.crew_panel``.
#:
#: The dynamic dashboard's verbs join on the same invariant. ``dashboard_fields``
#: is a read of the crewmate's own fields and mistake book. ``dashboard_write``
#: writes an agentic value into the calling crewmate's OWN dashboard, resolved from
#: the calling session exactly as ``panel_publish`` is, and every write is
#: type-checked against that crewmate's manifest before it lands. A prompt on
#: either would stall the unattended cycle the dashboard is refreshed from.
#:
#: The PAGE's four join them, and the bound is worth stating because these are the
#: first of this server's verbs that change what a person sees rather than what it
#: says. ``dashboard_templates`` is a read of the catalog. ``dashboard_preview``
#: records nothing at all: it stages a page beside the record, moves no version and
#: writes no history row, so the page somebody is reading is untouched.
#: ``dashboard_apply`` and ``dashboard_rollback`` do change the page -- and they are
#: granted rather than prompted on the same ownership test the tuple above is judged
#: by. The page is the calling crewmate's own, resolved from the calling session and
#: not from any argument; apply installs a template the catalog serves (or the one
#: staged by a preview); and every earlier version stays on disk, so the worst case
#: is a crewmate changing its own page and the person saying "go back".
#:
#: No approval step guards a page swap: a person cannot judge a page's script by
#: looking at it, so a preview gate adds a click and no safety. The frame contains
#: every page the same way, and the history makes every swap one rollback away.
_MEMBER_PANEL_GRANTS: tuple[str, ...] = (
    "@kirocrew-panel/panel_templates",
    "@kirocrew-panel/panel_publish",
    "@kirocrew-panel/dashboard_fields",
    # The data-type catalog, granted on the same test as ``dashboard_fields`` and with
    # less to weigh: it is a pure READ of the fold registry plus how far the calling
    # crewmate's own folds have been folded, it writes nothing and stages nothing, and
    # it takes no argument that could point it at another crewmate's logs. A prompt here
    # would stall the unattended cycle before the read that tells the agent which paths
    # exist -- which is the read that stops it guessing one.
    "@kirocrew-panel/dashboard_types",
    "@kirocrew-panel/dashboard_write",
    "@kirocrew-panel/dashboard_save",
    "@kirocrew-panel/dashboard_templates",
    "@kirocrew-panel/dashboard_preview",
    "@kirocrew-panel/dashboard_apply",
    "@kirocrew-panel/dashboard_rollback",
    # A picture of the calling crewmate's own page in force, granted on the read test
    # ``dashboard_fields`` passes: it writes, stages and versions nothing, takes no
    # argument at all, and the values it draws are the ones the crewmate's own tab and
    # ``dashboard_fields`` already show it.
    "@kirocrew-panel/dashboard_snapshot",
)


#: The kirocrew-core verbs the goal conductor may call WITHOUT an approval
#: prompt. Named one by one rather than as the whole ``@kirocrew-core`` server,
#: which put 74 registered core tools behind a single auto-approve entry. The
#: reason is the same one ``_CONDUCTOR_DASHBOARD_GRANTS`` states above and
#: ``_PIPELINE_CONDUCTOR_CORE_GRANTS`` restates below: this agent ingests
#: content it does not control (goal text, child-session transcripts, web
#: reads) on nudge-driven cycles with nobody at the keyboard, and a
#: server-wide grant let that content reach ``task_run``, ``workflow_run`` and
#: the ``spawn_*`` family — starting persistent work or a fleet of subagents
#: with no human in the loop.
#:
#: Dropping those three is not a new policy, it is the spec catching up with
#: the prompt: ``_CONDUCTOR_SYSTEM_PROMPT`` already PROHIBITS them by name ("a
#: work item never goes to ``spawn_run``, ``spawn_sub_agents``,
#: ``workflow_run`` or ``task_run``"), so a grant that auto-approved them
#: contradicted the charter it shipped with.
#:
#: DERIVED, not copied. The set is the union of the prompt's own "Your tools:"
#: inventory and the ``goal-conductor`` skill's real call sites, filtered to
#: the tools that actually register on ``kirocrew-core`` — the ``session_*``
#: and ``chat_folder_*`` verbs the charter also names are
#: ``@kirocrew-dashboard`` and are granted by the tuple above, while
#: ``list_sessions`` is core (``mcp_tools/sessions.py``) despite sitting in the
#: prompt's child-session paragraph. Deriving rather than reusing the sibling's
#: thirteen is load-bearing: ``select_crew`` is absent from that tuple and is
#: step 1 of this conductor's documented dispatch procedure
#: (``goal-conductor/SKILL.md``), so copying would have broken dispatch on the
#: first cycle while looking like a correct patch.
#:
#: ``select_crew`` earns its place under the invariant already stated for the
#: dashboard tuple — a granted verb may CREATE or READ, never MUTATE something
#: that already exists and is not the agent's own. ``_do_select_crew`` reads
#: config, resolves the crew's bindings, and appends one routing-decision
#: record keyed to its OWN session; it binds nothing and starts no work.
#:
#: What is granted: reads (``resource_status``, ``list_sessions``, skills), the
#: patrol loop's own lifecycle (``monitor_*``, ``autonudge_stop``, ``wait``),
#: the conductor's OWN durable ledger, routing (``select_crew``), and
#: reporting to the owner (``send_message``, ``send_notification``,
#: ``ask_question``), and the quiet end of a patrol cycle with nothing to report
#: (``nothing_to_do``, which mutates nothing and would otherwise prompt on every
#: quiet wake -- a blocking prompt in place of the noise it removes).
#: The work-ledger verbs the conductor may call without an approval prompt.
#: Per tool rather than the whole server, because the worker half is mounted on the
#: same server and a conductor has no reason to auto-approve a tool whose only
#: answer to it is a refusal. Both are on the same rule the dashboard grants
#: follow: the read only READS the conductor's own record, and the write only
#: touches fields the conductor owns on a ledger keyed to its own session — its
#: worst case in an unattended loop is bounded by the store's caps. Missing these
#: is not an error but a silent approval prompt on every patrol cycle, which is
#: why they are spelled out rather than left to the whole-server ref.
#:
#: The first two are the BASE every conductor spec ships
#: (``_CONDUCTOR_BASE_WORK_GRANTS``): every conductor records its items in the
#: ledger, because the dashboard's ``workstreams`` fold mints a board only from
#: ``work/recorded`` entries. This tuple is the goal conductor's superset, shipped
#: by ``_conductor_spec``, which both ``kirocrew-conductor`` and its deprecated
#: ``kirocrew-ledger-conductor`` alias call; the name stays because that alias is
#: still an installed spec.
#:
#: ``work_ledger_rebuild`` is the goal conductor's recovery step and no other
#: conductor's procedure names it. ``work_brief`` is the one worker-half verb
#: granted: it only READS the caller's own bound item (or answers ``not_bound``),
#: which is the same rule the two conductor verbs rest on. It is also a
#: second-level conductor's mandated FIRST call, in a child session nobody opened —
#: gated, that call is an approval stall before any planning happens.
#: ``work_report`` stays gated: it WRITES into the parent's record, across a
#: dispatch relationship.
_CONDUCTOR_BASE_WORK_GRANTS: tuple[str, ...] = (
    "@kirocrew-work/work_ledger_read",
    "@kirocrew-work/work_ledger_record",
)


_LEDGER_CONDUCTOR_WORK_GRANTS: tuple[str, ...] = _CONDUCTOR_BASE_WORK_GRANTS + (
    "@kirocrew-work/work_ledger_rebuild",
    "@kirocrew-work/work_brief",
)


#: The work-ledger verbs a WORKER may call without a prompt. A worker that must
#: ask permission to say it is blocked will not say it, and a report is the one
#: thing the whole design exists to make cheap.
_WORKER_WORK_GRANTS: tuple[str, ...] = (
    "@kirocrew-work/work_brief",
    "@kirocrew-work/work_report",
)


_CONDUCTOR_CORE_GRANTS: tuple[str, ...] = (
    "@kirocrew-core/monitor_start",
    "@kirocrew-core/monitor_update",
    "@kirocrew-core/autonudge_stop",
    "@kirocrew-core/wait",
    "@kirocrew-core/resource_status",
    "@kirocrew-core/list_sessions",
    "@kirocrew-core/session_ledger_read",
    "@kirocrew-core/session_ledger_record",
    "@kirocrew-core/skill_search",
    "@kirocrew-core/skill_fetch",
    "@kirocrew-core/select_crew",
    "@kirocrew-core/send_message",
    "@kirocrew-core/send_notification",
    "@kirocrew-core/ask_question",
    "@kirocrew-core/nothing_to_do",
    # Dropping its own chat at a round close. The directive consumer admits it
    # from a patrol wake only on the slot's own live self-armed loop, with no
    # question or approval pending (``session_directive_apply``).
    "@kirocrew-core/reset_conversation",
)


#: The auto-approve grants a worker must NOT hold even when the default agent does.
#: A worker exists for ONE work item and reports on that item; a recurring job
#: outlives the item, the session and the dispatch, so authoring one is not work a
#: worker can be doing on an item's behalf. ``cron_update`` and
#: ``cron_secret_request`` are here for the same reason rather than as a tidy
#: superset: rewriting an existing job's schedule or body is authoring a recurring
#: job by another route, and requesting vault secrets is requesting them FOR a
#: script job a worker may not create in the first place.
#:
#: The reading verbs the shipped template already grants — ``cron_list``,
#: ``cron_pause``, ``cron_resume``, ``cron_trigger``, ``cron_remove``,
#: ``cron_remove_all`` — are deliberately absent: acting on a job that already
#: exists is within an item's reach, and those are the worker's cron surface as it
#: stands.
#:
#: This withholds AUTO-APPROVE, not the tool. ``@kirocrew-cron`` stays in ``tools``
#: exactly as the default has it, so an excluded verb is still callable and simply
#: goes through the approval gate — the same shape the governance ceiling produces,
#: and the reason an item that genuinely needs a schedule can still ask a human for
#: one instead of failing silently.
_WORKER_EXCLUDED_GRANTS: frozenset[str] = frozenset(
    {
        "@kirocrew-cron/cron_add",
        "@kirocrew-cron/cron_update",
        "@kirocrew-cron/cron_secret_request",
    }
)


#: The kirocrew-core verbs the pipeline conductor may call WITHOUT an approval
#: prompt. Named one by one rather than as the whole ``@kirocrew-core`` server,
#: extending the dashboard-grants invariant below to the core surface: the
#: conductor ingests untrusted content (issue text, PR bodies) on unattended
#: cycles, and a server-wide grant would let that content start persistent
#: work (``task_run``, ``workflow_run``) or spawn arbitrary
#: subagents with no human in the loop. What is granted is reads
#: (``resource_status``, ``list_sessions``, skills), the conductor's OWN
#: patrol-loop lifecycle (``monitor_*``, ``autonudge_stop``, ``wait``), its
#: OWN durable ledger, reporting to the owner (``send_message``,
#: ``send_notification``, ``ask_question``), and the quiet end of a patrol
#: cycle with nothing to report (``nothing_to_do``, which mutates nothing). ``spawn_run`` — the intervention
#: ladder's read-only inspector — is deliberately NOT here: it starts agent
#: work from ingested context, so like ``session_send``/``session_stop`` it
#: stays mounted-but-gated and unattended runs get it from the operator's
#: session-level trust grant.
_PIPELINE_CONDUCTOR_CORE_GRANTS: tuple[str, ...] = (
    "@kirocrew-core/monitor_start",
    "@kirocrew-core/monitor_update",
    "@kirocrew-core/autonudge_stop",
    "@kirocrew-core/wait",
    "@kirocrew-core/resource_status",
    "@kirocrew-core/list_sessions",
    "@kirocrew-core/session_ledger_read",
    "@kirocrew-core/session_ledger_record",
    "@kirocrew-core/skill_search",
    "@kirocrew-core/skill_fetch",
    "@kirocrew-core/send_message",
    "@kirocrew-core/send_notification",
    "@kirocrew-core/ask_question",
    "@kirocrew-core/nothing_to_do",
)


#: The dashboard verbs the pipeline conductor may call WITHOUT an approval
#: prompt. Same tuple, same reasoning, as ``_CONDUCTOR_DASHBOARD_GRANTS``
#: above — the invariant (a granted verb may CREATE or READ, never MUTATE
#: workspace state that already exists and is not the agent's own; its worst
#: case in a loop must be bounded by the server) applies verbatim, because
#: this agent too ingests untrusted content by design: issue text and PR
#: bodies feed every granted verb on a nudge-driven cycle with nobody at the
#: keyboard. ``session_send`` / ``session_stop`` — which the patrol's
#: intervention ladder does use — stay mounted-but-gated for the same reason
#: they are gated on the goal conductor; unattended operation gets them via
#: the operator arming the conductor's own session in trust mode (the same
#: explicit, session-scoped human grant the worker sessions already require),
#: not via a standing spec-level bypass.
#: ``session_status`` rides along for the reason the goal conductor holds it: a pure
#: read of the caller's own children, narrower than the ``session_read_message`` grant
#: beside it, and asked on every unattended cycle. ``session_broadcast`` does NOT,
#: on ``session_send``'s withholding — the fan-out changes how many sessions ingested
#: text reaches, not whether anything bounds it.
_PIPELINE_CONDUCTOR_DASHBOARD_GRANTS: tuple[str, ...] = (
    "@kirocrew-dashboard/chat_folder_tree",
    "@kirocrew-dashboard/chat_folder_create",
    "@kirocrew-dashboard/session_create",
    "@kirocrew-dashboard/session_read_message",
    "@kirocrew-dashboard/session_status",
)


#: The security conductor's dashboard grants: the pipeline conductor's plus
#: ``chat_folder_file_self``, for the same reason the goal conductor holds it
#: (see ``_CONDUCTOR_DASHBOARD_GRANTS``): the verb writes only the caller's own
#: placement, and this agent's procedure files itself in the audit's folder
#: before its auditors go under ``<audit>/<agent>``. The pipeline conductor's
#: procedure does not file itself yet, so its tuple stays as it is rather
#: than carrying a grant nothing in its skill exercises.
_SECURITY_CONDUCTOR_DASHBOARD_GRANTS: tuple[str, ...] = _PIPELINE_CONDUCTOR_DASHBOARD_GRANTS + (
    "@kirocrew-dashboard/chat_folder_file_self",
)
