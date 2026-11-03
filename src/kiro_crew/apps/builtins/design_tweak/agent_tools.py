"""The agent tool Design Tweak declares, as data for the core's ``apps.builtin_agent_tools``.

A data module by contract: the core reads it in the ``kirocrew-core`` MCP server, the
validation registry and the gateway's internal-path table, so it imports nothing. What
each key means is documented on ``kiro_crew.apps.builtin_agent_tools.AgentTool``; the
reasons below are Design Tweak's.

``design_tweak_update_thread`` is the agent's ONLY credentialed path to this app's
``POST /thread`` route (the same pattern as ``ops_mission_control_api`` and
``issue_radar_record_investigation``: the MCP server process holds the internal secret,
and the agent never sees a credential). The app hands the agent batched click-anchored
comments to apply, but the agent cannot update the preview's thread bubbles itself:
posting to ``/thread`` needs a dashboard credential, and an agent session holds none
(cookies are httpOnly, the gateway mints its internal secret in-process and never puts
it in an agent's env, and the CLI credential mint is denied by the builtin
``credential-exfil`` rules). So the raw POST 403s, and the dots never change without
this tool.
"""

AGENT_TOOLS = {
    "app": "design-tweak",
    "tools": (
        {
            "name": "design_tweak_update_thread",
            "description": (
                "Report progress on one Design Tweak comment thread, so its dot "
                "in the live preview updates as you apply the batched edits. "
                "This is the ONLY way an agent session can update a thread: the "
                "raw `POST /thread` the skill once documented needs a dashboard "
                "credential the session does not hold (the credential-exfil "
                "safety floor blocks minting one), so it 403s and the dots never "
                "change. The MCP server process holds the credential and you "
                "never see one. Pass `request_id` (the request's id) and usually "
                "`comment_id` (the `cid` of the specific comment you are working) "
                "so the note lands on that comment's bubble; omit `comment_id` "
                "only for a note that spans the whole batch. Post a short `text` "
                "note when you start a comment and at each meaningful step; when "
                "that comment is finished, send `status='done'` so its dot turns "
                "green. `status` accepts only `done` — the tool reports forward "
                "progress and exposes no destructive thread action (it cannot "
                "clear or dismiss a request). `text` or `status` is required. "
                "Never write the request file yourself; progress is reported "
                "ONLY through this tool."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": (
                            "The request's id (the `id` the request file is "
                            "keyed by). Matches `^[A-Za-z0-9._-]+$`."
                        ),
                    },
                    "comment_id": {
                        "type": "string",
                        "description": (
                            "The `cid` of the specific comment to report against, "
                            "so the note lands on that comment's bubble. Omit for "
                            "a request-level note that spans the whole batch "
                            "(e.g. 'rebuilding, one moment'). Matches "
                            "`^[A-Za-z0-9._-]+$`."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "A single short progress line, e.g. 'Editing "
                            "styles.css — uppercasing .section-title'. Required "
                            "unless `status` is given."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["done"],
                        "description": (
                            "Set `done` to mark the comment finished so its dot "
                            "turns green. Only `done` is accepted. Required "
                            "unless `text` is given."
                        ),
                    },
                },
                "required": ["request_id"],
            },
            # Both ids are value-position only: the handler URL-quotes them with
            # ``safe=''`` so neither can carry a '/', '?' or '#' that would rewrite the
            # single ``/thread`` route, and the backend's own ``_ID_RE``
            # (``^[A-Za-z0-9._-]+$``) is the authority that rejects a malformed id. The
            # tool boundary therefore bounds only length, not grammar. The ``text or
            # status`` rule is likewise the backend's (``_h_thread``): an empty call is
            # refused there, so it is not restated here.
            "fields": (
                {"name": "request_id", "required": True, "max_len": 200},
                {"name": "comment_id", "max_len": 200, "default": ""},
                # Bounds one progress note. The backend caps a thread at
                # MAX_THREAD_ENTRIES short lines; 2 KiB is well above one legitimate
                # note and keeps an unbounded string from being carried further.
                {"name": "text", "max_len": 2_048, "default": ""},
                # Forward progress only. The backend's full set is ``new | sent |
                # done`` (COMMENT_STATUSES); ``new``/``sent`` are the app's own
                # lifecycle, not something an agent reports, so the tool admits only
                # ``done``. It exposes no destructive thread action: ``/clear``
                # archives a request and removes its pins, and is not reachable here.
                {"name": "status", "allowed": ("done",), "default": ""},
            ),
            # The one app route the agent reaches, through the gateway's app reverse
            # proxy (``/apps/{name}/api/{path}`` to this app's own backend process; NOT
            # ``/api/apps/...``, which carries only the gateway's own app management
            # routes). The gateway admits exactly ``/apps/design-tweak/api/thread`` to
            # internal-secret callers. Its match is ``path == p or path.startswith(p +
            # "/")``, so it admits ``/thread`` and ``/thread/<x>``, but NOT the sibling
            # state-mutating routes ``/submit``, ``/send``, ``/clear``, ``/delete``,
            # ``/delete-comment`` or the project and dev-server routes. A
            # ``/thread/<x>`` child is harmless: the backend matches ``route ==
            # "/thread"`` exactly, so a child path 404s there.
            "route": "/thread",
            "query": (("id", "request_id"), ("cid", "comment_id")),
            # The agent-authored note is passed through unchanged: the backend's
            # ``_redact_incoming_thread_text`` runs on EVERY ``/thread`` write, so a
            # credential or exfil URL in it is scrubbed before it persists. The core
            # handler redacts the response, which echoes the thread's earlier entries
            # and the app's own state.
            "body_constants": {"role": "agent"},
            "body_fields": ("text", "status"),
            "response_cap": 20_000,
        },
    ),
}
