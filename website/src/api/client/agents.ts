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
  /** Optional presentation label shown in place of `name`. `name` stays the
   *  identity every per-member route and binding is keyed on. */
  display_name?: string
  /** Crew origin, NORMALIZED by the server to exactly 'kirocrew' (created in
   *  the crew manager), 'builtin', or 'package' (agent-sync-installed; the
   *  legacy 'aim' spelling and any unknown value collapse to this). */
  source?: 'kirocrew' | 'builtin' | 'package' | string
  /** User's favourite mark; toggled via PUT /api/agents/{name}. */
  starred?: boolean
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
export interface CrewTeam {
  id: string
  name: string
  members: string[]
}

/** Free-form fields a crew publishes into its webview. The crew owns the shape,
 *  so every value is unknown until the renderer narrows it. */
export type CrewPanelData = Record<string, unknown>

/** Metadata half of GET /api/members/{slug}/panel. The document itself travels
 *  beside it as `html`, already composed server-side from the template. */
export interface CrewPanelMeta {
  template: string
  title: string
  crew: string
  published_at: string
  data: CrewPanelData
  /** The template's opt-in to render ITSELF in the docked card: the fixed pixel
   *  height of that compact frame, or null for a template that did not opt in
   *  (the drawer then keeps its native, zero-mint summary). */
  docked_height?: number | null
  /** The crew's superseded panels, newest last, one row per replaced publish.
   *  Bounded server-side; a file-only legacy record (published before the fold
   *  recorded history) omits this rather than sending an empty array. */
  history?: CrewPanelHistoryRow[]
  /** How many times this crew has published on this slot. Absent on a file-only
   *  legacy record for the same reason `history` is. */
  publishes?: number
  /** How many history rows aged out past the server's per-owner cap: the bound
   *  speaking, so a reader tells a history trimmed at its cap from a complete
   *  one. Absent on a file-only legacy record. */
  history_omitted?: number
}

/** One superseded panel in a crew's `CrewPanelMeta.history`. */
export interface CrewPanelHistoryRow {
  at: string
  title: string
  template: string
}

export function createAgentsEndpoints({ post, put, del, j, sessionKeyHeader: _sk }: ClientTransport) {
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
     *  `agent` resolves the configured default agent. */
    agentResolvedModel: (agent: string) =>
      fetch('/api/agents/resolved-model?agent=' + encodeURIComponent(agent)).then(j),
    /** Execution choices for a chat: configured members AND installed shared
     *  templates, each row tagged with its `selection_kind`. Read-only -- unlike
     *  the sync route it enrols nothing and allocates no member memory, so
     *  every picker can call it without side effects. Same `X-Session-Key`
     *  scoping as `kirocrewAgents`: project templates come from THIS chat's
     *  project, never from another open pane's. */
    agentCatalog: (sessionKey?: string) =>
      fetch('/api/agents/catalog', {
        headers: sessionKey ? { 'X-Session-Key': sessionKey } : { ..._sk },
      }).then(j) as Promise<{ agents: KiroCrewAgent[]; default_agent: string }>,
    createKirocrewAgent: (body: object) => post('/api/agents', body).then(j),
    // Crew Members page — roster of GLOBAL crews with DM-thread binding and the
    // cheap live-status fields the backend can answer without IO (richer live
    // detail rides the already-subscribed WS `slots` frames).
    members: () => fetch('/api/members').then(j) as Promise<{ members: MemberRosterRow[] }>,
    // Idempotent get-or-create of a member's pinned DM thread. Member slots are
    // born ONLY through this route (the generic slot-create endpoint refuses
    // mode="member"), so this is also the only place a member slot key comes from.
    memberThread: (slug: string) =>
      post('/api/members/' + encodeURIComponent(slug) + '/thread').then(j) as Promise<{ slot_key: string; slug: string; member: string }>,
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
    // The crew's published webview: metadata plus the composed document. Read
    // through this layer rather than a component-local `fetch`, like every sibling
    // above -- the members page's tests stub `api/client`, so a hand-rolled fetch was
    // the one reader they could not stub, and a silent fallback (a remembered crew
    // renamed away) surfaced as a red alert instead. `member` is the exact crew name
    // because the record carries an ownership claim the server checks against it;
    // slugs are lossy, so two crews can share one.
    memberPanel: (slug: string, member: string) =>
      fetch(
        '/api/members/' + encodeURIComponent(slug) + '/panel?member=' + encodeURIComponent(member),
      ).then(j) as Promise<{ panel: CrewPanelMeta | null; html: string | null }>,
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
        truncated: boolean
      }>,
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
    updateKirocrewAgent: (name: string, body: object) =>
      put('/api/agents/' + encodeURIComponent(name), body).then(j),
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
       * The whole pack, inlined. Read it through `hooks/usePackDetail` (a React
       * Query entry, `staleTime: Infinity`) rather than directly: this route
       * carries every file in the pack, so one read per pack per session is the
       * budget, and a grid or roster calling it per avatar would load N whole packs
       * to draw N frames. The crew avatar pays that one read per WORN pack to learn
       * each slot's format (the per-slot route cannot say it before the request);
       * `packDetailFrom` then keeps the bytes only for a Lottie slot, so the cache
       * never pins an svg or a base64 sheet no renderer reads from here. The three
       * shipped sample bundles are 1-4 KB; a content-free detail variant is the
       * follow-up if real packs prove otherwise.
       *
       * A renderer needs it because the FORMAT lives per slot — the player has to
       * be chosen before any bytes are requested, which the per-slot route
       * (`packSlotUrl`) cannot answer.
       */
      detail: (id: string) =>
        fetch('/api/appearances/' + encodeURIComponent(id)).then(j) as Promise<unknown>,
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
