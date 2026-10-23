"""The internal API path sets the dashboard's request barriers classify by.

Which paths only internal processes may call (loopback plus ``X-Internal-Secret``),
which the browser may also call, and the constants the deny-audit, CSRF and audit
layers read with them. Pure data: ``kiro_crew.dashboard.server`` imports every name
here and is the only module that does, so the server's binding is the one its owners
read and the one a test rebinds.
"""

from __future__ import annotations

# Strict internal API paths — exact paths that ONLY internal processes
# (mcp-core, doctor, cron) call, never the browser. Access requires loopback
# AND a matching ``X-Internal-Secret`` header; non-loopback is always denied and
# there is no cookie fall-through (see token_auth.token_auth_middleware).
#
# Module-level and shared by BOTH ``start_dashboard`` and ``start_api_server``
# so the two entrypoints can never drift: the ``--slack-only`` headless server
# must gate exactly the same MCP tool routes the dashboard does. Drift here —
# headless mounting no token auth at all — is an auth bypass. Keep this as the
# single source of truth.
_STRICT_INTERNAL_API_PATHS = frozenset(
    {
        "/api/send-message",
        "/api/delete-message",
        "/api/update-message",
        "/api/browser-event",
        "/api/browser/frame",
        "/api/browser/pump-audit",
        # Native browser command channel (agent->Electron). MACHINE endpoints,
        # same trust class as ``/api/browser/frame``: the MCP proxy posts commands
        # and the Electron main process long-polls/returns results, all loopback +
        # internal-secret. No browser calls them, so STRICT (not mixed). Each
        # handler re-asserts loopback because a ``local_only=False`` deployment
        # reclassifies strict paths as mixed.
        "/api/browser/command",
        "/api/browser/command-drain",
        "/api/browser/command-result",
        # External terminal grant redemption. Its only caller is the desktop
        # shell's main process, over the same channel as the browser command
        # drain above; the browser holds the grant but must never redeem it, so
        # STRICT (not mixed). The handler re-asserts ``internal_auth`` and
        # loopback because a ``local_only=False`` deployment reclassifies strict
        # paths as mixed.
        "/api/terminal/external-grant",
        # Computer use: the ``kirocrew-computer`` stdio shim's forwarding leg.
        # STRICT (not mixed): no browser calls it, and it is the entry point to
        # accessibility reads and input synthesis into the operator's real
        # applications — the one API surface where a cookie fall-through would be
        # a genuinely new attack path rather than a convenience. The Settings pair
        # (``/api/computer-use/config``) is deliberately NOT here: it is browser-
        # called and cookie-authed. Note the prefix-matching in
        # ``token_auth.middleware`` treats ``/api/computer-use/invoke/...`` as
        # strict too, which is correct — nothing else lives under it.
        "/api/computer-use/invoke",
        # Computer use: the live-view (PiP) frame ingress. STRICT for the same
        # reason as ``invoke`` — its body is a frame of the operator's own desktop
        # and its only caller is this gateway's own capture thread, so no browser
        # ever posts to it. The handler re-asserts loopback itself because a
        # ``local_only=False`` deployment reclassifies strict paths as mixed.
        "/api/computer-use/frame",
        "/api/session-keepalive",
        # Session search's Chinese dictionary words for mcp-core (the gateway
        # holds the host's one copy). MCP-only, no browser caller. The handler
        # re-asserts loopback AND ``internal_auth`` itself, because a missing
        # header falls through to cookie auth and a ``local_only=False``
        # deployment reclassifies strict paths as mixed.
        "/api/search/segment",
        # Blocking MCP ask_question: open / wait / withdraw. STRICT for the same
        # reason as the keepalive above -- the only caller is the tool in an MCP
        # subprocess, and the card is addressed by the attested X-Session-Key,
        # which a browser bearer must not be able to assert. Its own prefix so
        # the browser half of /api/ask-question is not swept in by the
        # prefix-matched strict check.
        "/api/agent-ask",
        # Session directives: the provider-neutral leg of the directive
        # protocol. STRICT for the same reasons as its sibling above — the
        # only legitimate caller is a Kiro Crew directive tool in an MCP
        # subprocess, and the route's whole point is that the payload arrives
        # somewhere the model's tool result is not trusted. A cookie
        # fall-through would let a browser bearer park a directive against a
        # session it merely has a tab on, bypassing the unix-socket peer check
        # that makes the declared X-Session-Key trustworthy.
        "/api/session-directive",
        # In-app update approval (RFC OQ7 step-up). STRICT: its only legitimate
        # caller is `kirocrew update approve` on the gateway host presenting the
        # trust/-fenced nonce plus X-Local-Secret; no browser ever posts to it —
        # the SPA can only ARM. Keeping it off the cookie fall-through means a
        # dashboard bearer cannot even reach the handler whose refusal is the
        # boundary, and the handler re-asserts host-locality itself because a
        # local_only=False deployment reclassifies strict paths as mixed.
        "/api/update/approve",
        # Flagged-file delivery approval step-up, the exact mirror
        # of /api/update/approve above and STRICT for the identical reason: its
        # only legitimate caller is `kirocrew file-delivery approve` on the gateway
        # host presenting the sandbox-masked nonce plus X-Internal-Secret. As with
        # update approve, "no browser ever posts to it -- the SPA can only ARM;
        # keeping it off the cookie fall-through means a dashboard bearer cannot
        # even reach the handler whose refusal is the boundary". The handler
        # (api_file_delivery_consent_approve -> _approve_is_local) re-asserts
        # host-locality itself, so the STRICT entry is the outer of two fences and
        # a local_only=False deployment that reclassifies strict paths as mixed is
        # still caught by the handler's own check.
        "/api/file-delivery/consent/approve",
        # Dev Fleet pod lifecycle — the agent surface behind the ``pod_up`` /
        # ``pod_down`` / ``pod_status`` / ``pod_ls`` MCP tools. An agent session
        # runs behind a sandbox with its own user namespace, so its shells cannot
        # connect the systemd user bus every pod verb needs; the gateway holds the
        # host bus and does the systemd part on the agent's behalf. Without these
        # entries the tools 403: an agent has no dashboard cookie, the gateway
        # mints its internal secret in-process and never puts it in an agent's
        # env, and ``.local_secret`` is on the sensitive-path denylist.
        #
        # STRICT, not mixed: no browser calls these. The dashboard's own pod
        # buttons go to the app backend through the ``/apps/dev-fleet/api/*``
        # reverse proxy, which is a different surface with cookie auth. Each
        # handler re-asserts loopback AND ``internal_auth`` itself, because a
        # ``local_only=False`` deployment reclassifies strict paths as mixed —
        # same reason ``/api/computer-use/frame`` re-asserts both.
        #
        # FOUR EXACT paths, never the ``/api/apps/dev-fleet/pod`` prefix. The
        # match is ``path == p or path.startswith(p + "/")``, so a prefix entry
        # would silently admit every future route under that segment — and this
        # app's neighbourhood includes worktree PRUNE and the Make Live cutover,
        # which must never become reachable by holding the internal secret.
        "/api/apps/dev-fleet/pod/up",
        "/api/apps/dev-fleet/pod/down",
        "/api/apps/dev-fleet/pod/status",
        "/api/apps/dev-fleet/pod/list",
        "/api/session-tool-policy",
        # NOTE: "/api/hooks/agent" is deliberately NOT here. It is an inbound
        # webhook for EXTERNAL callers (CI runners, review bots) that hold no
        # dashboard cookie and no gateway IPC secret, so a strict-internal entry
        # denies every real caller with 403 before the handler's own bearer check
        # can run, leaving the webhook token layer unreachable. It lives in
        # token_auth._BYPASS_EXACT_METHODS, scoped to POST, alongside the
        # /api/messaging/teams precedent: a self-authenticating external webhook
        # whose handler (api_hooks_agent -> _verify_hook_token) is the sole auth
        # gate. The POST scope matters — PUT/DELETE on that same literal path
        # match the {hook_id} wildcard of the dashboard-authed CRUD routes.
        "/api/outbox/notify",
        "/api/notifications/agent",  # MCP-only (send_notification tool); no browser caller
        "/api/slack/upload-file",
        "/api/channel/upload-file",
        "/api/slack/pins",
        "/api/slack/reactions",
        "/api/slack-profile",  # MCP-only (slack_profile tool); no browser caller
        "/api/sessions/summarize",  # MCP-only (list_sessions summarize leg); internal-secret, no browser caller
        # MCP-only (session_ledger_read / session_ledger_record tools); no
        # browser caller. Prefix matching covers "/api/session-ledger/record".
        # Without this entry the tools' internal-secret calls fall through to
        # cookie auth and are refused before the handler's own session
        # recognition can run.
        "/api/session-ledger",
        # MCP-only (the four kirocrew-work tools); no browser caller. Prefix
        # matching covers "/record", "/brief" and "/report". Without this entry
        # the tools' internal-secret calls fall through to cookie auth and are
        # refused before the handler's own session recognition can run.
        "/api/work-ledger",
        # MCP-only (the three kirocrew-crew-log read tools); no browser caller.
        # Prefix matching covers "/sessions", "/resolve" and every "/units/..."
        # sub-route. STRICT, not mixed, for the reason the session-control block
        # below gives: these read ANOTHER live session's recorded history, so a
        # forwarded browser must be hard-denied rather than fall through to a
        # cookie. Strict membership is NOT the whole gate -- a loopback request
        # with no secret header still reaches the handler through cookie auth --
        # so handlers/crew_log.py refuses a cookie-authed caller itself, and the
        # browser reads its own log through the cookie-only
        # "/api/sessions/{id}/crew-log" pair this entry does not cover.
        "/api/crew-log",
        # MCP-only (the five kirocrew-debug read tools); no browser caller at all.
        # Prefix matching covers "/gateway", "/refusals", "/threads", "/processes"
        # and "/snapshots". STRICT, not mixed, and the argument is stronger than the
        # crew log's: four of the five reads are HOST-WIDE (the interpreter's
        # threads, every process in the family, the recorded host series), so a
        # forwarded browser must be hard-denied rather than fall through to a
        # cookie. Strict membership is NOT the whole gate -- a loopback request with
        # no secret header still reaches the handler through cookie auth -- so
        # handlers/debug.py refuses a cookie-authed caller itself. Unlike the crew
        # log there is no cookie-only door to send it to: the dashboard has no debug
        # panel, so a browser has no door here.
        "/api/debug",
        # MCP-only (panel_publish / panel_templates tools); no browser caller --
        # the drawer READS through "/api/members/{slug}/panel", which is
        # registered by the same module a few lines below and deliberately NOT
        # under this prefix so it keeps cookie auth. Prefix matching covers both
        # "/api/agent-panel/publish" and "/api/agent-panel/templates". Same
        # wiring class as the ledger above: without this entry the
        # internal-secret call falls through to cookie auth and every publish
        # fails with 403.
        "/api/agent-panel",
        # MCP-only (the four kirocrew-guide tools); no browser caller. Prefix
        # matching covers "/actions", "/start", "/status" and "/cancel". STRICT:
        # the caller's slot is derived from the X-Session-Key this secret backs,
        # so a cookie fall-through would let a browser name any session as the one
        # offering the guide. The browser half ("/api/guide/pending", "/claim",
        # ...) is deliberately NOT under this prefix and stays on cookie auth.
        "/api/guide/agent",
        # MCP-only (the list_change_kinds / propose_change / get_change_status tools); no browser
        # caller. Prefix matching covers "/kinds", "/propose" and "/status".
        # STRICT for the same reason as the guide: the card's slot is derived from
        # the X-Session-Key this secret backs. The browser half ("/api/cards/pending",
        # "/api/cards/{id}/preview|cancel|dismiss") is deliberately NOT under this
        # prefix and stays on owner-only cookie auth; applying a card happens only
        # on the existing settings routes, through the card hook.
        "/api/cards/agent",
        # MCP-only (the kirocrew-mate update_self / set_* tools); no browser
        # caller. STRICT for the same reason as the guide and the cards: the
        # crewmate acted on is derived from the X-Session-Key this secret backs,
        # so a cookie fall-through would let a browser change any crewmate as
        # itself. The undo ("/api/members/{slug}/self-changes/{id}/undo") is the
        # user's click and stays on owner-only cookie auth, off this prefix.
        "/api/member/self",
        # MCP-only (knowledge_add_document tool); no browser caller — the
        # dashboard ingests via its own cookie-authed knowledge routes. Same
        # wiring class as "/api/notifications/agent" above.
        "/api/knowledge/agent-document",
        "/api/mcp/servers",
        # Session control -- the three routes behind the session_create /
        # session_stop / session_read_message MCP tools.
        # STRICT, not mixed: no browser calls them, and they are the entry point
        # to opening, stopping, and reading ANOTHER live conversation. A cookie
        # fall-through there would be a new authorization path, not a
        # convenience.
        #
        # Every route registered under /api/session-control MUST appear here.
        # An unlisted path falls through to the general branch, which honors only
        # cookie/query tokens, so the MCP caller's X-Internal-Secret is ignored
        # and the handler's own internal_auth re-assert then refuses it -- the
        # tool is unreachable in production while handler-level tests still pass.
        "/api/session-control/create",
        "/api/session-control/fork",
        "/api/session-control/stop",
        "/api/session-control/end-wait",
        "/api/session-control/retry",
        "/api/session-control/continue",
        "/api/session-control/set-model",
        "/api/session-control/reload",
        "/api/session-control/rename",
        "/api/session-control/generate-title",
        "/api/session-control/close",
        "/api/session-control/revive",
        "/api/session-control/history",
        "/api/session-control/send",
        "/api/session-control/broadcast",
        "/api/session-control/status",
        "/api/session-control/adopt",
        "/api/session-control/handoff",
        "/api/session-control/release",
        "/api/session-control/read",
        "/api/session-control/summary",
        # MCP-only structured monitor inspection. The caller selects its
        # session identity through X-Session-Key, so cookie authentication can
        # never authorize this leaf.
        "/api/autonudge/session-monitor",
    }
)


#: Statuses the deny-audit boundary treats as a permission decision. Deliberately
#: not "any 4xx": a 404 from routing and a 302 from host canonicalization are
#: outcomes, not refusals. Nothing raises 401 today (``token_auth_middleware``
#: RETURNS its 401/403 and audits each itself), but a barrier that raises one is
#: the same class of event as a raised 403, so it is covered by position too.
_PRE_AUDIT_DENY_STATUSES = frozenset({401, 403})


#: Suffix appended to an audited identity that reached the gateway THROUGH a
#: proxy rather than from the client itself. ``<name>_via_proxy`` means a
#: forwarder presented it.
#:
#: Read provenance from ``metadata["forwarded"]`` (``True`` / ``False``), not
#: from this suffix. Every ``api_access`` row written while a request is being
#: served records it: the auth and audit layers pass it explicitly, and a
#: handler's own row takes the verdict the deny-audit boundary binds for the
#: request (``kiro_crew.sel_request_provenance``). It means the same thing
#: whichever of them wrote the row. A row with no ``forwarded`` key has no
#: provenance recorded (one written outside any request, or by a background task
#: after its request ended), which is never "made directly". The suffix is kept,
#: unchanged, so existing readers and queries keyed on it keep matching.
_VIA_PROXY_SUFFIX = "_via_proxy"


#: Methods the CSRF barrier skips. A safe method does not mutate state, and
#: GET-based exfiltration is covered by the Host barrier above, which runs on
#: every method.
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# Mixed internal API paths — called by BOTH internal processes (loopback +
# ``X-Internal-Secret``) AND the browser (cookie auth), e.g. ``/api/spawn``
# polled by DCV/SSH-forwarded browsers. On non-loopback they perform explicit
# cookie validation (deny-by-default) rather than hard-denying, so forwarded
# browsers don't trip false "session expired" banners. Prefix-matched:
# ``path == p or path.startswith(p + "/")``. Shared by both entrypoints.
_MIXED_INTERNAL_API_PATHS = frozenset(
    {
        # Called by MCP (loopback + secret) AND browser polling
        # (DCV/SSH-forwarded cookie auth).  See token_auth.py.
        "/api/spawn",
        # The update step-up's arm record: POST (arm), GET (status), DELETE
        # (decline). Two callers, two credentials: the About panel polls it with
        # a cookie, and an agent asking for an app update presents
        # X-Internal-Secret. EXACT path — a sibling of the STRICT
        # `/api/update/approve`, never its prefix: token_auth matches `p` or
        # `p + "/"`, so `/api/update` here would turn a host-only approval into
        # a cookie-reachable one. Arming grants nothing (the record carries no
        # nonce and no endpoint installs from it), so a mixed admission widens
        # nothing.
        "/api/update/arm",
        "/api/chat",
        "/api/lessons",
        # MCP recall still requires the handler's protected member/session proof.
        "/api/memory/recall",
        "/api/crons",  # CLI cron trigger; prefix covers all sub-routes (consistent with spawn/taskrunner)
        # The cron_add/cron_update MCP tools resolve-or-create Schedule-page
        # folders via X-Internal-Secret. Same trap as "/api/artifact-folders"
        # below: token_auth prefix-matching is (path == p or
        # path.startswith(p + "/")), so "/api/cron-folders" is NOT covered by
        # the "/api/crons" entry above — without this entry those MCP calls
        # fall through to cookie auth and fail with "Token required".
        "/api/cron-folders",
        "/api/taskrunner",
        "/api/artifacts",
        # The 5 artifact_folder_* MCP tools authenticate via X-Internal-Secret.
        # token_auth prefix-matching is (path == p or path.startswith(p + "/")),
        # so "/api/artifact-folders" is NOT covered by the "/api/artifacts"
        # entry above — without this entry those MCP calls fall through to
        # cookie auth and fail with "Token required".
        "/api/artifact-folders",
        # Provider-routed remote-artifact browse/clone/fork. Same auth model
        # as "/api/artifacts": browser cookie auth + internal-secret callers;
        # prefix covers every /api/remote-artifacts/{provider}/... sub-route.
        "/api/remote-artifacts",
        "/api/workflows",  # DW engine: MCP tools + Workflows tab polling
        "/api/deploy",  # MCP deploy_artifact tool — server enforces preview-only (confirm/override_scan stripped for internal-secret callers)
        # Issue Radar investigation record — the ONE app route reachable with the
        # internal secret, for the ``issue_radar_record_investigation`` MCP tool.
        # An investigating chat agent has no dashboard token (cookies are
        # httpOnly, the gateway mints its internal secret in-process and never
        # puts it in an agent's env, and ``.local_secret`` is on the
        # ``security.py`` sensitive-path denylist), so the PUT the Investigate
        # seed prompt asks for would 403 unconditionally and no investigation
        # could record its findings. Deliberately the FULL path, not the
        # ``/api/apps/issue-radar`` prefix: prefix-matching here would also admit
        # the app's GitHub/GitLab WRITE routes (label, close/reopen, comment) to
        # anything holding the internal secret. This route is local-only triage
        # state — no forge write, no shared ledger.
        "/api/apps/issue-radar/investigation",
        # Ops Mission Control agent surface — the routes the app's SOP-driven
        # crons and investigation slots call through the ``ops_mission_control_api``
        # MCP tool (the app's ONLY credentialed agent path; same trust model as
        # ``/api/apps/issue-radar/investigation`` above: agents hold no cookie,
        # no gateway IPC secret, and the CLI credential mint is denied by the
        # builtin ``credential-exfil`` rules — deliberately, see security.py).
        # Enumerated EXACT paths, never the app prefix: prefix-matching
        # ``/api/apps/ops-mission-control`` would also admit provider
        # configuration/secret writes, ``/settings``, the external ``/webhook``
        # ingest and the human-only ``/incident/proposal/decide`` route to
        # anything holding the internal secret. Bare ``/incident`` is excluded
        # for the same reason (this matcher is exact-or-prefix, so admitting it
        # would admit ``/incident/propose`` and ``/incident/proposal/decide``);
        # single-incident reads go through ``/incidents?id=`` instead. The
        # ``/rotation`` and ``/ledger`` entries DO cover their sub-routes
        # (``/rotation/arm``, ``/ledger/contradictions``, ``/ledger/hygiene``)
        # — all agent-surface by design.
        "/api/apps/ops-mission-control/state",
        "/api/apps/ops-mission-control/signals",
        "/api/apps/ops-mission-control/incidents",
        "/api/apps/ops-mission-control/handover",
        "/api/apps/ops-mission-control/rotation",
        "/api/apps/ops-mission-control/ledger",
        "/api/apps/ops-mission-control/dispatch",
        "/api/apps/ops-mission-control/incident/transition",
        "/api/apps/ops-mission-control/incident/claim",
        "/api/apps/ops-mission-control/incident/action",
        # Issue Radar crew ledger — the read leg and the work-item write leg, for
        # the ``issue_radar_crew_read`` / ``issue_radar_crew_record`` MCP tools. A
        # crew agent has no dashboard token (same three reasons as the
        # investigation entry above), and the ledger is the ONLY thing that
        # survives its compaction, its per-turn ceiling and a gateway restart, so
        # without these entries an unattended crew has no memory at all.
        #
        # FULL paths, never the ``/api/apps/issue-radar`` prefix — for the reason
        # spelled out on the investigation entry: prefix-matching there would also
        # admit the app's GitHub/GitLab WRITE routes (label, close/reopen,
        # comment) to anything holding the internal secret.
        #
        # Read this pair as ONE admission, not two. Matching is
        # ``path == p or path.startswith(p + "/")``, so the ``/crew`` entry
        # already covers ``/crew/work`` and EVERY future ``/crew/...`` sub-route:
        # anything added under that segment becomes agent-reachable the moment it
        # is routed, with no further edit here. So a forge-write or destructive
        # route must not live under ``/crew/`` — put it on its own path, or refuse
        # an internal-secret caller at the handler the way
        # ``api_skills_discover_install`` does below.
        "/api/apps/issue-radar/crew",
        # Redundant under the prefix match above; kept explicit so a reader sees
        # both routes the crew tools actually call.
        "/api/apps/issue-radar/crew/work",
        # Design Tweak thread progress — the ONE app route reachable with the
        # internal secret, for the ``design_tweak_update_thread`` MCP tool. The
        # app hands the agent batched click-anchored comments and the agent
        # applies them, but it cannot update the preview's comment-thread
        # bubbles: posting to ``/thread`` needs a dashboard credential, and an
        # agent session holds none (cookies are httpOnly, the gateway mints its
        # internal secret in-process and never puts it in an agent's env, and
        # the CLI credential mint is denied by the
        # builtin ``credential-exfil`` rules — the same trust model as the
        # ``/api/apps/issue-radar/investigation`` and Ops Mission Control
        # entries above). So the documented raw POST 403s unconditionally and
        # the dots never change; this admission is what lets the scoped tool
        # carry progress through.
        #
        # ONE path, never the ``/apps/design-tweak`` prefix. ``internal_path_matches``
        # admits an entry AND its children (``path == p or path.startswith(p + "/")``),
        # so this entry admits ``/thread`` and ``/thread/<x>`` — but NOT the app's
        # sibling state-mutating routes ``/submit``, ``/send``, ``/clear`` (archives
        # a request and removes its pins), ``/delete``, ``/delete-comment`` or the
        # project/dev-server management routes, which are separate entries this set
        # does not list. A ``/thread/<x>`` child is harmless: Design Tweak's backend
        # matches ``route == "/thread"`` exactly, so a child path 404s there. The
        # agent only needs to append thread progress, so only ``/thread`` is admitted.
        #
        # The path is the gateway's reverse-proxy route ``/apps/{name}/api/{path}``
        # (``apps/routes_proxy.py`` ``handle_app_api_proxy``), NOT ``/api/apps/...``:
        # Design Tweak runs its ``_h_thread`` in its own backend process reached
        # only through that proxy, unlike Issue Radar / Ops Mission Control which
        # register aiohttp routes directly under ``/api/apps/<name>``.
        "/apps/design-tweak/api/thread",
        # Registry skill discovery — the READ leg only, for the
        # ``skill_discover`` / ``skill_fetch`` MCP tools. The Skills page calls
        # the same two routes with cookie auth, hence mixed rather than strict.
        #
        # Prefix-matching (path == p or startswith(p + "/")) means the first
        # entry ALSO admits ``/api/skills/-/discover/install`` — a WRITE that
        # fetches third-party files and writes them into the skills dir. That is
        # closed off at the handler instead: ``api_skills_discover_install``
        # refuses an internal-secret caller outright (see its ``internal_auth``
        # guard), so installation stays a deliberate human action in the
        # dashboard. Do not remove that guard to add an install MCP tool without
        # re-reviewing this admission.
        "/api/skills/-/discover",
        # Redundant under the prefix match above, kept explicit so a reader of
        # this list sees both routes the MCP tools actually call.
        "/api/skills/-/discover/preview",
        "/v1/chat/completions",  # OpenAI-compat API
    }
)
