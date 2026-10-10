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
  /** Created on the dashboard: `source` is `kirocrew` AND the record carries
   *  a member id. Listed on the roster unasked. Absent on an older gateway. */
  dashboard_created?: boolean
  /** The Crewmates-page DM thread holds at least one message (a live slot's
   *  unflushed rows included). Listed on the roster unasked. Absent on an
   *  older gateway. */
  has_dm_message?: boolean
  /** Epoch seconds the USER last sent this crew a message, in its DM or in a
   *  normal chat; 0 = never. Background work (crons, wakes, sub-agents,
   *  dispatched workers, apps) never moves it. The Crewmates list shows and
   *  orders by it. Absent on an older gateway. */
  last_chat_ts?: number
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

/** A dashboard template's manifest. Mirrors `TemplateManifest`. */
export interface DashboardManifest {
  id: string
  version: number
  title: string
  description: string
  source: 'builtin' | 'user' | 'shared'
  fields: Record<string, DashboardFieldSpec>
}

/**
 * The dashboard artifact a page is drawn from, as the READ names it.
 *
 * A DESCRIPTOR and not the package content. The browser never receives
 * `model` / `view` / `theme`: the document is composed server-side from them, so
 * what the page needs is the artifact's identity, the `version` a patch's
 * `layout` is compared against, and the binding -- which is what makes "is this
 * page still this crewmate's" answerable without shipping the layout.
 *
 * `DashboardPackage` in `types/dashboardPackage.ts` is the content type, for the
 * Artifacts library and anything that reads the artifact itself.
 */
export interface DashboardPackageRef {
  /** The dashboard artifact's slug. */
  slug: string
  /** The artifact's version. This is the `layout` a block patch carries, and a
   *  change to it means the page must be recomposed rather than patched. */
  version: number
  /** The canonical fingerprint of `model` + `view` + `theme`. The controller
   *  compares it alongside the version, because the two disagreeing is how a
   *  rewritten layout at the same version would otherwise go unnoticed. */
  layout_fingerprint: string
  /** `crewmate:<slug>` or `session:<slot key>`. */
  bound_to: string
}

/**
 * `GET /api/members/{slug}/dashboard?member=<name>` -- the crewmate's dashboard.
 *
 * ADDITIVE, which is the one thing to understand about this type. The controller
 * (D2, chat-2622) serves a v3 PACKAGE body when the crewmate has a readable
 * dashboard package, and the pre-existing template-registry body otherwise --
 * the template registry is still a live feature and this round does not retire
 * it. A reader tells the two apart by ONE key: `package` present means the v3
 * body, and every v3-only key below travels with it.
 *
 * The page's own rule is narrower than this type and does not vary with it:
 * `state === 'live'` with a `rendered_html` is the only thing that draws, so a
 * body from either path that reports anything else gets the empty state rather
 * than a page. That is where "no default page" is enforced on this side.
 */
export interface MemberDashboardRead {
  /** `live`, `empty`, `stale` or `error`. The ONLY field that decides whether a
   *  page draws. `stale` belongs to the template path alone -- it means the
   *  registry moved past a copy, and a package has no registry behind it. */
  state: 'empty' | 'live' | 'stale' | 'error'
  /** One sentence naming why, for a non-live state. Never shown raw to a reader:
   *  the frame's own copy says what happened, and this is for a log. */
  state_reason?: string
  /** The composed page, values already filled in. Absent when the renderer
   *  refused or the state is not one it composes. */
  rendered_html?: string

  // -- the v3 package path. Present together, keyed by `package`. ------------ //

  /** The dashboard artifact behind this page, or absent on the template path. */
  package?: DashboardPackageRef
  /** Where the block-patch stream stood when this body was composed, so the
   *  FIRST patch after a read is checkable for a gap like every later one. */
  push_version?: number
  /** The WS message type a patch for this page arrives as, named BY THE SERVER
   *  so the seam is the shape rather than a constant spelled in two languages.
   *  The router still needs a static case to dispatch on, so the frontend holds
   *  the constant too -- and checks it against this, which turns a rename into a
   *  visible disagreement instead of a push path that silently stops. */
  push_frame?: string
  /**
   * The TWO postMessage types the document listens for, both named by the server
   * so the frontend need hold neither.
   *
   * `page_message` is the FULL PAINT: that listener replaces the whole read and
   * re-initialises every block. `page_patch_message` is the narrow one a fold
   * push forwards. They are different strings on purpose -- using the full-paint
   * one for a fold advance gives a block that owns a canvas a second canvas, with
   * two scenes animating over each other, and the server has a test asserting the
   * two values differ.
   *
   * A push forwards the frame's `patch` VERBATIM and that object carries its own
   * `type`, so `page_patch_message` is here for the comparison rather than to be
   * put on a message this side builds.
   */
  page_message?: string
  page_patch_message?: string
  /** Block id -> field name -> value: every block's values, which IS the whole
   *  page in data. The page reads the KEYS as the set of blocks it is showing,
   *  because `view.blocks` itself never crosses to the browser. */
  blocks?: Record<string, Record<string, unknown>>
  /** Field names whose fold path did not resolve. Named rather than sent as a
   *  null, which would render as a zero. */
  missing?: string[]

  // -- the template path, unchanged. ---------------------------------------- //

  /** The crewmate's instance version on the template path. */
  instance_version?: number
  /** Which template the instance copied. */
  template?: { id: string; version: number }
  /** The stored copy. NEVER mounted -- see `CrewDynamicDashboard`. */
  html?: string
  /** The manifest of the template the instance copied. */
  manifest?: DashboardManifest
}

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
    // Mate's once-only first greeting. The server decides: it starts one
    // Mate turn only for Mate's own empty pinned thread, at most once
    // ever, and answers every other case with the outcome that declined it.
    memberGreet: (slug: string) =>
      post('/api/members/' + encodeURIComponent(slug) + '/greet').then(j) as Promise<{ outcome: string }>,
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
    // The crewmate's dynamic dashboard: its LAYOUT PACKAGE and the page composed
    // from it. Read here because the frame beside the chat is what renders it.
    //
    // There is no template registry on this path any more. The dashboard is a
    // `kind="dashboard"` artifact holding `bound_to`, `model`, `view` and `theme`,
    // and `package_version` is that artifact's version -- which moves only when the
    // layout does, because a dashboard package carries no values. So there is no
    // "stored copy" beside a "composed page" to choose between, and no builtin page
    // to fall back to: a crewmate with no package has NO dashboard, which the
    // `empty` state says and the frame's empty state draws.
    //
    // `package` and `rendered_html` travel together for the reason the manifest and
    // the page used to: the composed document binds blocks by id and fields by
    // name, and the package is what says which of those fields the crewmate wrote
    // itself (`source.agentic`) rather than read out of a fold. Two reads could pair
    // a document with a package from a different version, and the page would then
    // address a push at a block the layout no longer places.
    //
    // `member` is the exact crew name, as every member route takes it (slugs are lossy).
    // `locale` is the UI language the page should render its own words in; the
    // gateway checks it against the shipped catalogs and falls back to English.
    /** `preview`: the STAGED page `dashboard_preview` set aside, which no version
     *  records; 404 `no_preview` once nothing is staged. */
    memberDashboard: (slug: string, member: string, locale = '', preview = false) =>
      fetch(
        '/api/members/' + encodeURIComponent(slug) + '/dashboard?member=' + encodeURIComponent(member)
          + (locale ? '&locale=' + encodeURIComponent(locale) : '')
          + (preview ? '&preview=1' : ''),
      ).then(j) as Promise<MemberDashboardRead | null>,
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
    // The work a crewmate holds (open goals, recent sessions), read for its
    // cold-start welcome. `member` is the exact crew name (slugs are lossy).
    memberRecap: (slug: string, member: string) =>
      fetch(
        '/api/members/' + encodeURIComponent(slug) + '/recap?member=' + encodeURIComponent(member),
      ).then(j) as Promise<MemberRecap>,
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
