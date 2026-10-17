/**
 * Crew agents: installed agents, agent templates and spec
 * detail/patch/fork/publish/reset, Kiro Crew agent CRUD, the catalog and
 * resolved model, the members roster and drawer reads, crew teams, appearance
 * packs and avatar upload.
 */

import type { KiroCrewAgent } from '../../components/AgentSelector'
import type { ProjectionsBlock } from '../../state/memberProjectionTypes'
import type { ClientTransport } from './transport'

/** One row of GET /api/members — a global crew as a Crew Members roster entry.
 *  Crew-record fields (kiro_agent, workspace, memory_store, model, …) are
 *  spread verbatim from the backend dataclass; only the fields the page reads
 *  are typed here, and extras pass through untyped by design so a new backend
 *  field is not a frontend break. */
export interface MemberRosterRow {
  /** Crew name — the display identity and the agent the DM thread pins to. */
  name: string
  /** Stable path-safe slug deriving the member dir and the slot key. */
  slug: string
  /** The pinned DM thread's slot key ('' until first open / unbound). */
  slot_key: string
  /** O(1) liveness: the bound slot is mid-turn right now. */
  running: boolean
  /** Epoch seconds of the DM transcript's last write; 0 = never talked. */
  last_active_ts?: number
  last_message?: string
  /** True when the DM thread's NEWEST event is a Stop press. The server skips
   *  the stop card's raw JSON from `last_message`, so the preview is the last
   *  conversational line — which reads as ongoing work on a thread the user has
   *  stopped. This locale-independent boolean lets the roster render a localized
   *  "Stopped" chip beside that preview; the word itself is never sent from the
   *  server, where the client's locale is unknown. Omitted (not `false`) when
   *  the newest event is not a stop, and absent again once a newer
   *  conversational row lands. */
  last_message_stopped?: boolean
  kiro_agent?: string
  workspace?: string
  memory_store?: string
  memory_version?: number
  memory_owner?: string
  model?: string
  /** The crewmate's own effort pin; '' inherits. */
  reasoning_effort?: string
  /** What the sessions this crewmate opens through `session_create` run on
   *  when the call names no model: '' = the crewmate's own model, 'auto' = the
   *  backend default (a real pin here), else a listed model. */
  worker_model?: string
  /** Same for the reasoning effort; '' = the crewmate's own level. */
  worker_reasoning_effort?: string
  /** Optional presentation label shown in place of `name`. `name` stays the
   *  identity every per-member route and binding is keyed on. */
  display_name?: string
  /** Crew origin, NORMALIZED by the server to exactly 'kirocrew' (created in
   *  the crew manager), 'builtin', or 'package' (agent-sync-installed; the
   *  legacy 'aim' spelling and any unknown value collapse to this). */
  source?: 'kirocrew' | 'builtin' | 'package' | string
  /** User's favourite mark; toggled via PUT /api/agents/{name}. */
  starred?: boolean
  /** Created on the dashboard: `source` is `kirocrew` AND the record carries
   *  a member id. Listed on the roster unasked. Absent on an older gateway. */
  dashboard_created?: boolean
  /** The Crewmates-page DM thread holds at least one message (a live slot's
   *  unflushed rows included). Listed on the roster unasked. Absent on an
   *  older gateway. */
  has_dm_message?: boolean
  /** Mate's row only: its once-only welcome marker is claimed (the welcome was
   *  sent or attempted). Absent on every other row, when the marker cannot be
   *  checked, and on an older gateway; the Crewmates page then treats Mate as
   *  already seen. */
  welcome_claimed?: boolean
  /** Mate's row only: a first welcome is owed (the gateway recorded one when
   *  it created Mate). Absent or false = never owed; the Crewmates page then
   *  treats Mate as already seen. */
  welcome_owed?: boolean
  /** Baseline projections (roster/activity/wake/driving) at a known seq, fed
   *  to the per-member projection store so the page renders from pushed
   *  frames. Absent on an older gateway that predates the event log. */
  projections?: ProjectionsBlock
  [extra: string]: unknown
}

/** One entry of GET /api/members/{slug}/activity — a recorded engagement.
 *  `via` distinguishes a session the user opened with the member ('chat')
 *  from an orchestrator routing decision ('select_crew'); the latter records
 *  intent, not a run. */
export interface MemberActivityEntry {
  /** Epoch seconds (UTC) the engagement was recorded. */
  ts: number
  via: 'chat' | 'select_crew' | string
  project?: string
}

/** One team of crewmates (GET /api/teams). `members` are exact crew NAMES in
 *  the user's order; a crewmate is on at most one team, which the store
 *  enforces on every write. */
/** GET /api/members/{slug}/recap: the work a crewmate holds, for its cold-start
 *  welcome. Goals still open (in its thread or a recent session), then the
 *  newest other sessions that ran as it. */
export interface MemberRecap {
  slug: string
  member: string
  paused: { goal: string; next: string }[]
  recent: { title: string; ts: number }[]
}

/** One key result: how the crewmate will know its goal is reached. */
export interface GoalKeyResult {
  id: string
  text: string
  /** A number ("27", "80%") or a word ("yes"). */
  target: string
  /** The crewmate's latest reading, free text (a number or a short note); '' until it first reads it. */
  current: string
  /** Epoch seconds of the latest reading; 0 when never read. */
  updated_at: number
  /** Every reading of the last 90 days, oldest first. */
  readings?: { value: string; at: number }[]
  /** The ids of the goals this milestone serves (none, one or several). */
  goal_ids: string[]
}

/** One of the crewmate's goals. `number` is its 1-based place in the list. */
export interface MemberGoal {
  id: string
  number: number
  text: string
  created_at: number | null
  updated_at: number | null
  updated_by: 'user' | 'crewmate' | null
}

/** The check-in switches and their shared timing. weekday: 0 = Sunday. */
export interface GoalSchedules {
  time: string
  timezone: string
  daily: boolean
  weekly: boolean
  weekday: number
  monthly: boolean
  month_day: number
}

/** A saved weekly report or monthly review (an artifact). */
export interface GoalReport {
  kind: 'weekly' | 'monthly'
  slug: string
  title: string
  at: number
}

/** GET /api/members/{slug}/goals: the crewmate's goal record. */
export interface MemberGoals {
  slug: string
  member: string
  /** The crewmate's goals, in list order (no cap on how many). */
  goals: MemberGoal[]
  key_results: GoalKeyResult[]
  schedules: GoalSchedules
  reports: GoalReport[]
  /** The key result the user removed last, restorable (with its readings) until the set changes again. */
  removed_key_result: { id: string; text: string; at: number } | null
  /** The goal the user removed last, restorable (with its milestone links). */
  removed_goal: { id: string; text: string; at: number } | null
  /** The goal a write added or changed (write answers only). */
  goal_id?: string
  limits: {
    goal_text_max_chars: number
    key_result_text_max_chars: number
    key_result_value_max_chars: number
    key_result_reading_max_chars: number
  }
}

export interface CrewTeam {
  id: string
  name: string
  members: string[]
}

/** Metadata half of GET /api/members/{slug}/panel. The document itself travels
 *  beside it as `html`, already composed server-side from the template. */
/** One field of a dashboard template manifest, as the gateway serves it.
 *
 * `source` is the whole point of the manifest reaching the browser: a `fold`
 * field is a number the gateway read out of the crew log, and an `agentic` one is
 * a value the crewmate asserted. The frame marks the second kind, because a
 * reader deciding whether to act on a number is entitled to know which it is.
 *
 * Mirrors `FieldSpec` in `src/kiro_crew/dashboard_templates/manifest.py`. */
export interface DashboardFieldSpec {
  type: 'number' | 'string' | 'boolean' | 'array' | 'object'
  source: { agentic: true } | { fold: string; path: string }
}

/** One dynamic-dashboard read, as both dashboard routes answer it.
 *
 *  Declared once because the two routes serve the SAME body out of the same handler
 *  module -- a crewmate's by slug, a root session's by slot -- so two spellings of it
 *  would be two things to keep in step, and a reader comparing them would have to work
 *  out whether a field missing from one is absent or merely unwritten. */
export interface DashboardRead {
  instance_version: number
  template: { id: string; version: number }
  html: string
  /** The live page with its values already filled in by the gateway (data island +
   *  bootstrap + page), absent when the fill failed or the state is not live. The frame
   *  prefers it; the raw `html` is the fallback. */
  rendered_html?: string
  manifest: DashboardManifest
  state: 'empty' | 'live' | 'stale' | 'error'
  /** Every field this page can ever be sent, off the package's own model. */
  fields?: string[]
  /** The `kind="dashboard"` package this response rendered, when the binding resolved
   *  to one. The tab carries it back on a refetch and drops any pushed frame belonging
   *  to a different package. Absent where no package is bound, which is every root
   *  session today: only a crewmate carries a binding. */
  dashboard?: string
  /** The person's layout lock: while true the crewmate cannot apply or roll back a page. */
  structure_locked?: boolean
  /** Set on a `preview` read: this body is the staged page, not the record. */
  preview?: boolean
  /** Set on a `preview` read: which staged page this is. Keep sends it back. */
  staged_ms?: number
  /** The crewmate's tab shows the SUGGESTIONS page instead of this read's page: the
   *  gateway's one decision (`dashboard_agentic.shows_suggestions`), which the
   *  crewmate's turn block is told too. Absent on every other read. */
  suggestions?: boolean
}

/** A dashboard template's manifest. Mirrors `TemplateManifest`. */
export interface DashboardManifest {
  id: string
  version: number
  title: string
  description: string
  source: 'builtin' | 'user' | 'shared'
  fields: Record<string, DashboardFieldSpec>
}

export function createAgentsEndpoints({ post, put, del, j, jfetch: fetch, sessionKeyHeader: _sk }: ClientTransport) {
  const crew = {
    // Agents
    agentsInstalled: () => fetch('/api/agents/installed').then(j),
    // The Agent templates tab: roster with editability + references, create, delete.
    // Editing goes through `agentPatch` (description, prompt, tools, allowedTools,
    // model, skills); the server refuses the definition keys on a read-only spec
    // (409 template_read_only) and a delete on a referenced one (409
    // template_referenced, body.references lists what).
    agentTemplates: () => fetch('/api/agents/templates').then(j),
    agentTemplateCreate: (body: { name: string; description?: string; from?: string }) => post('/api/agents/templates', body).then(j),
    agentTemplateDelete: (name: string) => fetch('/api/agents/detail/' + encodeURIComponent(name), { method: 'DELETE' }).then(j),
    agentDetail: (name: string) => fetch('/api/agents/detail/' + encodeURIComponent(name)).then(j),
    agentPatch: (name: string, body: object) => fetch('/api/agents/detail/' + encodeURIComponent(name), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j),
    agentFork: (name: string, crew: string) => fetch('/api/agents/detail/' + encodeURIComponent(name) + '/fork', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ crew }) }).then(j),
    agentPublish: (name: string, crew: string, newName: string) => fetch('/api/agents/detail/' + encodeURIComponent(name) + '/publish', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ crew, name: newName }) }).then(j),
    // Rebind the crew to `name`'s origin AND delete the private copy in one atomic
    // server call, so a reset can no longer end half-done (rebound but copy kept, or
    // vice versa). May reject with origin_missing / stale_binding / not_a_private_copy
    // / ambiguous_template_name / rebind_failed.
    agentReset: (name: string, crew: string) => fetch('/api/agents/detail/' + encodeURIComponent(name) + '/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ crew }) }).then(j),
    // Kiro Crew agents
    // sessionKey identifies the CHAT SLOT whose project scope applies. The
    // server resolves project-local agents through
    // active_project_dir(state, session_key); with no key it falls back to
    // "the single project shared by every slot" and fails closed when two
    // slots sit on different projects, so project-scoped agents silently
    // vanish from the picker. Surfaces with no slot context (Channels,
    // Schedule) pass nothing and keep the global-only view.
    kirocrewAgents: (sessionKey?: string) =>
      fetch('/api/agents', {
        headers: sessionKey ? { 'X-Session-Key': sessionKey } : { ..._sk },
      }).then(j),
    /** The model a new session on this Kiro Crew agent would run on. Empty
     *  `agent` resolves the configured default agent; `app` adds the owning
     *  app's own model setting. */
    agentResolvedModel: (agent: string, app?: string) =>
      fetch('/api/agents/resolved-model?agent=' + encodeURIComponent(agent)
        + (app ? '&app=' + encodeURIComponent(app) : '')).then(j),
    /** Execution choices for a chat: configured members AND installed shared
     *  templates, each row tagged with its `selection_kind`. Read-only -- unlike
     *  the sync route it enrols nothing and allocates no member memory, so
     *  every picker can call it without side effects. Same `X-Session-Key`
     *  scoping as `kirocrewAgents`: project templates come from THIS chat's
     *  project, never from another open pane's. */
    agentCatalog: (sessionKey?: string) =>
      fetch('/api/agents/catalog', {
        headers: sessionKey ? { 'X-Session-Key': sessionKey } : { ..._sk },
      }).then(j) as Promise<{
        agents: KiroCrewAgent[]
        default_agent: string
        /** The harness this chat (no key: a new session) runs, config spelling. */
        session_backend?: string
        /** The harnesses that refuse a markdown-only agent. */
        markdown_refused_backends?: string[]
        /** The harnesses that load a markdown agent spec. */
        markdown_spec_backends?: string[]
      }>,
    /** `extra` carries per-request headers (a guided create's `X-Guide-*`);
     *  it rides THIS request only, never the shared transport. */
    createKirocrewAgent: (body: object, extra?: Record<string, string>) =>
      (extra ? post('/api/agents', body, undefined, extra) : post('/api/agents', body)).then(j),
    // Crew Members page — roster of GLOBAL crews with DM-thread binding and the
    // cheap live-status fields the backend can answer without IO (richer live
    // detail rides the already-subscribed WS `slots` frames).
    members: () => fetch('/api/members').then(j) as Promise<{ members: MemberRosterRow[] }>,
    // Idempotent get-or-create of a member's pinned DM thread. Member slots are
    // born ONLY through this route (the generic slot-create endpoint refuses
    // mode="member"), so this is also the only place a member slot key comes from.
    memberThread: (slug: string) =>
      post('/api/members/' + encodeURIComponent(slug) + '/thread').then(j) as Promise<{ slot_key: string; slug: string; member: string }>,
    // A crewmate's once-only first greeting. The server decides: it starts one
    // turn only on the empty pinned thread of Mate or of a crewmate whose
    // create asked for it (`first_greeting`), at most once
    // ever, and answers every other case with the outcome that declined it.
    memberGreet: (slug: string) =>
      post('/api/members/' + encodeURIComponent(slug) + '/greet').then(j) as Promise<{ outcome: string }>,
    // Fresh start on a crewmate's pinned thread: stop any turn, then drop the
    // conversation so the next turn starts clean. Same slot key; the
    // transcript stays. `reset_at` is the server's time of the reset; `outcome`
    // is `queued` when the session was still busy and the clear waits for it.
    // `reset_persisted` is false when the clear landed but the fold boundary
    // failed to append to the member log -- the clear itself is never retried
    // for that fault, so the page's own in-session fold (set from `reset_at`
    // regardless) is what the user sees; only a reload loses it.
    memberFreshStart: (slug: string) =>
      post('/api/members/' + encodeURIComponent(slug) + '/fresh-start').then(j) as Promise<{ slot: string; reset_at: string; outcome: 'cleared' | 'queued'; reset_persisted: boolean }>,
    // A member's recent activity pointers (real recorded signal only: session
    // participations and routing decisions). `member` is the exact crew name —
    // slugs are lossy, so the backend filters the shared log by exact name.
    // Fetched on drawer open, never polled.
    memberActivity: (slug: string, member: string) =>
      fetch(
        '/api/members/' + encodeURIComponent(slug) + '/activity?member=' + encodeURIComponent(member),
      ).then(j) as Promise<{
        slug: string
        member: string
        /** True when the display window is saturated — derived counters are floors. */
        capped: boolean
        entries: MemberActivityEntry[]
      }>,
    // The open member's folded projection views. The roster list carries only the
    // `roster` view each list row paints; the drawer paints activity, wake and
    // driving, and it is open for one member at a time, so it reads the whole block
    // here rather than making every row in the list carry three views nothing on it
    // reads. `member` is the exact crew name because the server checks it against
    // the log's own header (slugs are lossy, so two crews can share one).
    memberProjections: (slug: string, member: string) =>
      fetch(
        '/api/members/' + encodeURIComponent(slug) + '/projections?member=' + encodeURIComponent(member),
      ).then(j) as Promise<ProjectionsBlock>,
    // The crewmate's dynamic dashboard INSTANCE: which template it copied, its own
    // edited page, the manifest that says where each field's value comes from, and
    // the instance's state. Served by the registry (contract v3 part 4/7) and read
    // here because the frame beside the chat is what renders it.
    //
    // `html` and `manifest` travel TOGETHER on purpose: the page binds fields by
    // name and the manifest is what says which of those names are the crewmate's
    // own writes rather than folded numbers. Two reads could pair a page with a
    // manifest from a different instance version, and the frame would then mark the
    // wrong cells as agentic -- which is the one thing this surface must not get
    // wrong, because it is the reader's only signal of how much to trust a number.
    // `member` is the exact crew name, as every member route takes it (slugs are lossy).
    // `locale` is the UI language the page should render its own words in; the
    // gateway checks it against the shipped catalogs and falls back to English.
    // `preview` reads the STAGED page instead of the live record -- the page an agent
    // offered and nobody has applied. Same route, same owner gate; nothing staged
    // answers 404 `no_preview`.
    memberDashboard: (slug: string, member: string, locale = '', preview = false) =>
      fetch(
        '/api/members/' + encodeURIComponent(slug) + '/dashboard?member=' + encodeURIComponent(member)
          + (locale ? '&locale=' + encodeURIComponent(locale) : '')
          + (preview ? '&preview=1' : ''),
      ).then(j) as Promise<DashboardRead | null>,
    // The person's Keep: install exactly the staged page the preview read named by
    // `staged_ms`. Owner-only, refused to an agent's internal-secret caller, 409
    // `preview_changed` when another page was staged since, 423 `dashboard_locked`.
    memberDashboardPreviewApply: (slug: string, member: string, stagedMs: number) =>
      post('/api/members/' + encodeURIComponent(slug) + '/dashboard/preview/apply?member=' + encodeURIComponent(member), { staged_ms: stagedMs })
        .then(j) as Promise<{ ok: boolean; instance_version: number; template_id: string }>,
    // The person's Discard: drop the staged page the preview read named. Same gates
    // as Keep; 409 `preview_changed` when another page was staged since, ok when
    // nothing is staged any more.
    memberDashboardPreviewDiscard: (slug: string, member: string, stagedMs: number) =>
      post('/api/members/' + encodeURIComponent(slug) + '/dashboard/preview/discard?member=' + encodeURIComponent(member), { staged_ms: stagedMs })
        .then(j) as Promise<{ ok: boolean }>,
    // Set or clear that lock. Owner-only on the gateway, and refused to an agent's
    // internal-secret caller: only the person, from the crewmate's Profile card, can move it.
    memberDashboardLock: (slug: string, member: string, locked: boolean) =>
      post('/api/members/' + encodeURIComponent(slug) + '/dashboard/lock?member=' + encodeURIComponent(member), { locked })
        .then(j) as Promise<{ ok: boolean; structure_locked: boolean }>,
    // A ROOT session's own dynamic dashboard, the same body as `memberDashboard`.
    // Keyed by the slot: the gateway reads every field from that session's own work
    // ledger and crew log, and answers 404 `not_root_session` for a dispatched or
    // adopted one.
    sessionDashboard: (slot: string, locale = '', preview = false) => {
      const p = new URLSearchParams()
      if (locale) p.set('locale', locale)
      if (preview) p.set('preview', '1')
      const query = p.toString()
      return fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/dashboard' + (query ? '?' + query : '')).then(j) as Promise<DashboardRead | null>
    },
    /** The whole read, after the page refused a patch it could not place in order.
     *
     *  The host's half of the frame's gap rule. A page applies patches in sequence
     *  and asks for this when one skips a version -- applying it anyway would show
     *  one field from after a change and the rest from before it, with nothing on the
     *  page saying so. 409 `no_live_page` when nothing is armed; the remedy is to
     *  re-read the dashboard, which arms one. */
    memberDashboardRefetch: (slug: string, member: string, dashboard: string) =>
      fetch(
        '/api/members/' + encodeURIComponent(slug) + '/dashboard/refetch?member='
          + encodeURIComponent(member) + '&dashboard=' + encodeURIComponent(dashboard),
        { method: 'POST' },
      ).then(j) as Promise<{ dashboard?: string; read: { [key: string]: unknown } } | null>,
    // The crewmate's self-maintained briefing markdown. Read-only from the UI
    // (no editor: the file is agent-written and edited where the crewmate keeps
    // it). `member` is the exact crew name (slugs are lossy).
    memberBriefing: (slug: string, member: string) =>
      fetch(
        '/api/members/' + encodeURIComponent(slug) + '/briefing?member=' + encodeURIComponent(member),
      ).then(j) as Promise<{
        slug: string
        member: string
        /** Whether the platform can read the file safely (false on Windows). */
        supported: boolean
        /** Markdown content, or empty string when the crewmate has not written notes yet. */
        text: string
        /** Last-modified timestamp, or null when no notes file exists yet. */
        updated_ts: number | null
        /** The text above was redacted on the way out (a secret-like string or an
         *  exfiltration URL replaced by its placeholder); the panel says so above
         *  the notes. */
        redacted: boolean
        /** The file ran past the briefing cap, so the text above ends in the
         *  truncation marker instead of the tail; the panel says so above the
         *  notes. */
        /** SYSTEM-owned, beside the agent's notes: what the crewmate is told about
         *  its Dashboard page -- the pointer line its prompt carries, each field it
         *  writes with what it means / when / how (and a helper script path where the
         *  data lives outside the crew log), and the helper names -- or null when it
         *  has no page. Absent from an older gateway. */
        upkeep?: {
          pointer: string
          fields: { field: string; means: string; when: string; how: string; helper?: string }[]
          scripts: string[]
        } | null
        truncated: boolean
      }>,
    // The work a crewmate holds (open goals, recent sessions), read for its
    // cold-start welcome. `member` is the exact crew name (slugs are lossy).
    memberRecap: (slug: string, member: string) =>
      fetch(
        '/api/members/' + encodeURIComponent(slug) + '/recap?member=' + encodeURIComponent(member),
      ).then(j) as Promise<MemberRecap>,
    // A crewmate's goals (the Goals tab). Every write is an owner action and
    // answers the whole record; `member` is the exact crew name (slugs are lossy).
    memberGoals: (() => {
      const base = (slug: string) => '/api/members/' + encodeURIComponent(slug) + '/goals'
      const q = (member: string) => '?member=' + encodeURIComponent(member)
      return {
        get: (slug: string, member: string) => fetch(base(slug) + q(member)).then(j) as Promise<MemberGoals>,
        /** Add a goal (no goal_id) or replace goal `goalId`'s text, `expected` being the text it replaces. */
        setGoal: (slug: string, member: string, text: string, goalId?: string, expected?: string) =>
          put(base(slug) + '/long-term', goalId ? { member, long_term: text, goal_id: goalId, expected } : { member, long_term: text }).then(j) as Promise<MemberGoals>,
        removeGoal: (slug: string, member: string, id: string) =>
          del(base(slug) + '/items/' + encodeURIComponent(id) + q(member)).then(j) as Promise<MemberGoals>,
        restoreGoal: (slug: string, member: string, id: string) =>
          post(base(slug) + '/items/' + encodeURIComponent(id) + '/restore', { member }).then(j) as Promise<MemberGoals>,
        setKeyResults: (slug: string, member: string, items: { id?: string; text: string; target: string; current?: string; goal_ids?: string[] }[], expected?: { text: string; target: string }[]) =>
          put(base(slug) + '/key-results', { member, items, expected }).then(j) as Promise<MemberGoals>,
        removeKeyResult: (slug: string, member: string, id: string) =>
          del(base(slug) + '/key-results/' + encodeURIComponent(id) + q(member)).then(j) as Promise<MemberGoals>,
        restoreKeyResult: (slug: string, member: string, id: string) =>
          post(base(slug) + '/key-results/' + encodeURIComponent(id) + '/restore', { member }).then(j) as Promise<MemberGoals>,
        setSchedules: (slug: string, member: string, changes: Partial<GoalSchedules>) =>
          put(base(slug) + '/schedules', { member, ...changes }).then(j) as Promise<MemberGoals>,
      }
    })(),
    // Crewmate teams: a name plus an ordered member list, stored by the gateway
    // in the data home's crew-teams directory. Dashboard-only like the members routes; the three
    // writes are owner actions. `remove` rather than `delete`: a reserved word
    // reads badly as a method name at every call site.
    teams: {
      list: () => fetch('/api/teams').then(j) as Promise<{ teams: CrewTeam[] }>,
      create: (body: { name: string; members: string[] }) =>
        post('/api/teams', body).then(j) as Promise<{ team: CrewTeam }>,
      update: (id: string, body: { name?: string; add?: string[]; remove?: string[] }) =>
        put('/api/teams/' + encodeURIComponent(id), body).then(j) as Promise<{ team: CrewTeam }>,
      remove: (id: string) => del('/api/teams/' + encodeURIComponent(id)).then(j) as Promise<{ ok: boolean }>,
    },
    updateKirocrewAgent: (name: string, body: object) => {
      // Names with characters that need URL encoding (e.g. spaces → %20) are
      // sent via the body-form route PUT /api/agents so a path-normalising
      // proxy cannot decode the path segment to an HTTP delimiter and return 502.
      if (encodeURIComponent(name) !== name) {
        return put('/api/agents', { ...body, name }).then(j)
      }
      return put('/api/agents/' + encodeURIComponent(name), body).then(j)
    },
    deleteKirocrewAgent: (name: string) =>
      del('/api/agents/' + encodeURIComponent(name)).then(j),
    /** Stage a crew's picture on the server (a `.pending` file only — the
     *  config PUT with `avatar: {kind:'image'}` is what promotes it live,
     *  keeping the editor's Apply→Save two-step a real commit point). */
    /**
     * The crew appearance library — the packs a crew can wear.
     *
     * Owner-gated, same-origin cookie auth.
     */
    appearances: {
      list: () => fetch('/api/appearances').then(j) as Promise<{ packs?: unknown }>,
      /**
       * One pack. Read it through `hooks/usePackDetail` (a React Query entry,
       * `staleTime: Infinity`) rather than directly, so a grid or roster shares one
       * read per pack per session however many avatars wear it.
       *
       * A renderer needs it because the FORMAT lives per slot — the player has to
       * be chosen before any bytes are requested, which the per-slot route
       * (`packSlotUrl`) cannot answer. `meta: true` (`?meta=1`) answers exactly
       * that, without any file's bytes: each slot's format, the sprite geometry
       * and the cue presence. Without it the route inlines every file in the pack,
       * which only a Lottie slot reads — its document is parsed in the page.
       */
      detail: (id: string, opts?: { meta?: boolean }) =>
        fetch('/api/appearances/' + encodeURIComponent(id) + (opts?.meta ? '?meta=1' : ''))
          .then(j) as Promise<unknown>,
      /** Install an exported pack. The JSON envelope, not multipart: the bundle is
       *  already parsed client-side to reject an obviously wrong pick, so posting
       *  it back as a file would only re-serialize what we hold. */
      importBundle: (bundle: unknown) =>
        post('/api/appearances/import', { bundle }).then(j) as Promise<{
          ok?: boolean
          id?: string
          error?: string
        }>,
      /** Delete a custom pack. Rejects 409 while a crew wears it, and the rejection
       *  body names those crews — `force` is deliberately NOT exposed. */
      remove: (id: string) =>
        del('/api/appearances/' + encodeURIComponent(id)).then(j) as Promise<{
          ok?: boolean
          id?: string
        }>,
    },
    uploadCrewAvatar: (name: string, file: Blob) => {
      const form = new FormData()
      form.append('file', file, 'avatar.png')
      return fetch('/api/agents/' + encodeURIComponent(name) + '/avatar', {
        method: 'POST',
        body: form,
      }).then(j) as Promise<{ ok?: boolean; staged?: boolean; token?: string; error?: string }>
    },
  }

  return { crew }
}
