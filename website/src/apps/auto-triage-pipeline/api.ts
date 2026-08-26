// Auto Triage Pipeline — a small, SELF-CONTAINED typed client for the one seam
// this app reads: Issue Radar's crew-fabric endpoint. The data half of the
// feature (recording `phase` on the ledger line, `fold_fabric`, the route) lives
// in the `issue_radar` builtin and is repo-agnostic; this app is its first
// tenant, so it reads THROUGH that seam rather than owning a backend of its own.
//
//   GET /api/apps/issue-radar/crew/fabric?owner=&repo=[&provider=&host=]
//
// The base path is issue-radar's, matching every builtin's `/api/apps/<name>`
// convention. Nothing here imports from `../issue-radar/*`: the types below are
// this app's own copy, so the two apps can evolve their frontends independently
// (wave 2 builds the faithful drawing and the queue dashboard on top of these).
//
// FORWARD-TOLERANT, like the seam it reads. `crewFabric()` never throws on a repo
// with no fabric: a 404 (issue-radar disabled / route absent), 500, 403 (repo not
// connected), a non-JSON body, or a payload from a newer schema all collapse to
// the same normalized empty result, and the view renders its designed empty
// state. The EMPTY case is the COMMON one — most installs never ran a crew.

/** The issue-radar API base — the seam owner. */
const ISSUE_RADAR_API = '/api/apps/issue-radar'

/** The provider a repo lives on. Mirrors issue-radar's `SourceProvider`; kept as
 * a plain string union so this app carries no dependency on that module. */
export type SourceProvider = 'github' | 'gitlab' | 'azure'

/** The repo a fabric request is scoped to. `provider`/`host` ride on the request
 * because a `group/project` path names a different project on gitlab.com than on
 * a self-managed instance; both optional so a value persisted before GitLab
 * support still loads (absent = public GitHub). */
export interface RepoRef {
  owner: string
  repo: string
  provider?: SourceProvider
  host?: string
}

/** Every phase a work item can be in, in lifecycle order — mirrors
 * `crew_store.PHASES` and issue-radar's `CREW_PHASES`. The pure fold derives the
 * on-spine subset from this, so it cannot drift from the enum. */
export const CREW_PHASES = [
  'selected',
  'claimed',
  'investigating',
  'implementing',
  'awaiting-ci',
  'addressing-review',
  'awaiting-merge',
  'awaiting-reply',
  'resolved',
  'skipped',
  'yielded',
  'handed-back',
  'preempted',
] as const

export type CrewPhase = typeof CREW_PHASES[number]

/** The CI rollup the fold flattens onto a work item, for a lane's badge/tooltip.
 * Open-ended: the store merges whatever the crew recorded. `state` is the coarse
 * verdict a view colours by. */
/** One point on a work item's timeline: it ENTERED `phase` at `at` (ISO-8601).
 * In TIME order and MAY repeat a phase — a review round-trip
 * (`awaiting-ci` → `addressing-review` → `awaiting-ci`) is three entries, the
 * last re-entering `awaiting-ci`. `at` may be absent on a legacy line written
 * before the store recorded the phase; that degrades the dwell math (no
 * duration) rather than breaking the fold. */
export interface CrewFabricTimelineEntry {
  phase: CrewPhase
  at?: string | null
}

/** Where a lane LEFT the spine, set only when the live `phase` is off-spine
 * (`skipped` / `yielded` / `handed-back` / `preempted` / `awaiting-reply`).
 * Drawn as a stub OFF the lane, never a column. */
export interface CrewFabricExit {
  phase: CrewPhase
  at?: string | null
}

/** One folded work item = one lane. `phase` is the item's LIVE phase and is
 * AUTHORITATIVE: a view must render the head at `phase`, never at `timeline`'s
 * max index — a round-trip ends left of where it has been. */
export interface CrewFabricItem {
  number: number
  crew_id: string
  /** The issue/PR's REAL title, seeded server-side from the issues/pulls list
   * caches Issue Radar already keeps (zero extra API cost). Empty string when the
   * number was never cached / aged out — the lane then shows its id alone. This is
   * NEVER the crew's `next` intent; that lives under `next`. */
  title: string
  /** The crew's resumable INTENT for this item ("add the Windows branch to
   * _safe_chmod") — what it is about to do next, NOT a title. Empty string when
   * the crew recorded none. Kept distinct from `title` so a view can show either
   * without one masquerading as the other. */
  next: string
  /** Null when the item has no PR (a plain rect rather than a chamfered chip). */
  pr_number: number | null
  phase: CrewPhase
  timeline: CrewFabricTimelineEntry[]
  /** Set only when `phase` is off-spine (see `CrewFabricExit`). */
  exit?: CrewFabricExit | null
  /** How many times the item re-entered the spine after an exit. 0/absent when
   * it never did. */
  reopens?: number
}

/** `GET /crew/fabric` response. `phases` is the phase enum IN ORDER, served by
 * the fold so a drawing's columns cannot disagree with the ledger. A non-GitHub
 * provider, or a repo with no crews, answers `items: []` at HTTP 200 — and this
 * client SYNTHESIZES the same shape for a 404/500/parse failure. */
export interface CrewFabricResponse {
  schema: number
  owner: string
  repo: string
  provider?: SourceProvider
  host?: string | null
  /** ISO-8601 when the fold ran, so a view can time an open dwell against it
   * rather than the browser clock. Absent in the synthesized-empty case. */
  generated_at?: string | null
  phases: CrewPhase[]
  items: CrewFabricItem[]
}

/** The fabric schema version this client was written against — mirrors
 * `crew_store.FABRIC_SCHEMA` and issue-radar's `CREW_FABRIC_SCHEMA`. */
export const CREW_FABRIC_SCHEMA = 1

/** A repository connected in Issue Radar's config — one row of the switcher this
 * app now resolves its repo against. This is the backend source of truth: the
 * stored preference below is only a REMEMBERED CHOICE, and a preference that no
 * longer appears in this list is stale and must fall back. Mirrors issue-radar's
 * `ConnectedRepo` but is this app's own copy so nothing imports from
 * `../issue-radar/*` (a pure HTTP contract is the only coupling). */
export interface ConnectedRepo {
  owner: string
  repo: string
  /** Absent on records written before GitLab support — treat as 'github'. */
  provider?: SourceProvider
  /** Absent on legacy records — treat as 'github.com'. */
  host?: string
  enabled?: boolean
}

/** `GET /repos` response — the connected-repo list this app falls back to when it
 * has no valid stored preference. */
export interface ReposResponse {
  repos: ConnectedRepo[]
}

/** This app's OWN localStorage key for the repo the user last viewed here. It is
 * a REMEMBERED PREFERENCE, not the source of truth — the connected-repo list from
 * the backend is authoritative, and a preference naming a repo no longer in that
 * list is discarded (see `lib/fabric.ts` `selectRepo`). The app writes only this
 * key; it never writes Issue Radar's. */
export const REPO_PREFERENCE_KEY = 'kc:auto-triage-pipeline:repo'

/** localStorage key Issue Radar persists its active repo under. This app READS it
 * (never writes it) as a seed for a first-ever visit, so a user who already has a
 * repo open in Issue Radar lands on the same one — but it is only one candidate
 * preference, not the source of truth. */
export const ISSUE_RADAR_ACTIVE_REPO_KEY = 'kc:issue-radar:active-repo'

/** Coerce an unknown parsed value into a `RepoRef`, or null if it is not one.
 * Guards every field so a malformed or pre-GitLab value cannot crash the read. */
function coerceRepoRef(v: unknown): RepoRef | null {
  if (!v || typeof v !== 'object') return null
  const o = v as Record<string, unknown>
  if (typeof o.owner !== 'string' || typeof o.repo !== 'string') return null
  if (!o.owner || !o.repo) return null
  const ref: RepoRef = { owner: o.owner, repo: o.repo }
  if (typeof o.provider === 'string') ref.provider = o.provider as SourceProvider
  if (typeof o.host === 'string') ref.host = o.host
  return ref
}

/** Read a `RepoRef` out of a localStorage key, or null when absent/invalid. */
function readRepoRefKey(key: string): RepoRef | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    return coerceRepoRef(JSON.parse(raw))
  } catch {
    return null
  }
}

/** The remembered repo preference for THIS app, or null. Prefers this app's own
 * key; falls back to Issue Radar's active-repo key so a first-ever visit with a
 * repo already open there lands on the same one. Both are only candidate
 * preferences — `selectRepo` still validates the choice against the connected
 * list and falls back when it is stale. */
export function loadStoredPreference(): RepoRef | null {
  return readRepoRefKey(REPO_PREFERENCE_KEY) ?? readRepoRefKey(ISSUE_RADAR_ACTIVE_REPO_KEY)
}

/** Persist the user's chosen repo under THIS app's own key. Never touches Issue
 * Radar's key. Best-effort: a storage failure (private mode, quota) is swallowed
 * — the choice simply is not remembered across reloads. */
export function saveRepoPreference(ref: RepoRef): void {
  try {
    const v: RepoRef = { owner: ref.owner, repo: ref.repo }
    if (ref.provider) v.provider = ref.provider
    if (ref.host) v.host = ref.host
    localStorage.setItem(REPO_PREFERENCE_KEY, JSON.stringify(v))
  } catch {
    // ignore — persistence is a nicety, not a requirement
  }
}

/** The query params a fabric request carries — owner/repo plus the identity
 * (provider/host) when present. */
function repoQuery(ref: RepoRef): Record<string, string> {
  const q: Record<string, string> = { owner: ref.owner, repo: ref.repo }
  if (ref.provider) q.provider = ref.provider
  if (ref.host) q.host = ref.host
  return q
}

export const autoTriagePipelineApi = {
  /**
   * Fetch the folded crew fabric for a repo. Never throws on "no data yet": a
   * transport failure, any non-2xx, a non-JSON body, or a payload missing the
   * fields the view reads all normalize to an empty result the view draws its
   * designed empty state for.
   */
  crewFabric: async (ref: RepoRef): Promise<CrewFabricResponse> => {
    const empty = (): CrewFabricResponse => ({
      schema: CREW_FABRIC_SCHEMA,
      owner: ref.owner,
      repo: ref.repo,
      provider: ref.provider,
      host: ref.host ?? null,
      generated_at: null,
      phases: [...CREW_PHASES],
      items: [],
    })
    let r: Response
    try {
      const q = new URLSearchParams(repoQuery(ref))
      r = await fetch(`${ISSUE_RADAR_API}/crew/fabric?${q.toString()}`, {
        credentials: 'same-origin',
      })
    } catch {
      // Transport-level failure (offline / DNS): nothing was answered, so this is
      // "no data yet" too, not a thrown error the caller must special-case.
      return empty()
    }
    if (!r.ok) return empty()
    let body: unknown
    try {
      body = await r.json()
    } catch {
      return empty()
    }
    if (!body || typeof body !== 'object') return empty()
    const b = body as Partial<CrewFabricResponse>
    if (!Array.isArray(b.items)) return empty()
    return {
      schema: typeof b.schema === 'number' ? b.schema : CREW_FABRIC_SCHEMA,
      owner: b.owner ?? ref.owner,
      repo: b.repo ?? ref.repo,
      provider: b.provider ?? ref.provider,
      host: b.host ?? ref.host ?? null,
      generated_at: b.generated_at ?? null,
      phases: Array.isArray(b.phases) && b.phases.length > 0 ? b.phases : [...CREW_PHASES],
      items: b.items,
    }
  },

  /**
   * List the repositories connected in Issue Radar — the backend source of truth
   * this app resolves its repo against (see `lib/fabric.ts` `selectRepo`). Reuses
   * Issue Radar's own `GET /repos`; no new endpoint is invented.
   *
   * FORWARD-TOLERANT like `crewFabric`: a transport failure, any non-2xx (route
   * absent / Issue Radar disabled), a non-JSON body, or a payload without a
   * `repos` array all collapse to `[]` — i.e. "no repo connected", which is the
   * genuine empty state the view renders.
   */
  listConnectedRepos: async (): Promise<ConnectedRepo[]> => {
    let r: Response
    try {
      r = await fetch(`${ISSUE_RADAR_API}/repos`, { credentials: 'same-origin' })
    } catch {
      return []
    }
    if (!r.ok) return []
    let body: unknown
    try {
      body = await r.json()
    } catch {
      return []
    }
    if (!body || typeof body !== 'object') return []
    const raw = (body as { repos?: unknown }).repos
    if (!Array.isArray(raw)) return []
    const out: ConnectedRepo[] = []
    for (const e of raw) {
      if (!e || typeof e !== 'object') continue
      const o = e as Record<string, unknown>
      if (typeof o.owner !== 'string' || typeof o.repo !== 'string') continue
      if (!o.owner || !o.repo) continue
      const entry: ConnectedRepo = { owner: o.owner, repo: o.repo }
      if (typeof o.provider === 'string') entry.provider = o.provider as SourceProvider
      if (typeof o.host === 'string') entry.host = o.host
      if (typeof o.enabled === 'boolean') entry.enabled = o.enabled
      out.push(entry)
    }
    return out
  },
}

// ---------------------------------------------------------------------------
// L0 / L1 / L2 — the app's OWN three read endpoints.
//
// These hit this app's own backend (`/api/apps/auto-triage-pipeline/*`, folded
// by `pipeline_fold.py`), not Issue Radar's seam above. The `to_dict()` payloads
// in that module ARE the contract mirrored here.
//
// Same forward-tolerant law as `crewFabric`: the folds read a LIVE, append-only
// log another process is writing, so EVERY field can be absent, null, or the
// wrong type on a partial payload. The types below describe the well-formed
// shape; the clients COERCE every field defensively and never throw — a
// transport failure, any non-2xx, a non-JSON body, or a partial/newer-schema
// payload all collapse to a designed empty result the view can render.
// ---------------------------------------------------------------------------

/** This app's own backend base — distinct from `ISSUE_RADAR_API` above. */
const ATP_API = `/api/apps/${'auto-triage-pipeline'}`

/** Unit a step's throughput is counted in. Early steps are batch jobs that open
 * no session, so they count issues; the session-bearing steps count sessions.
 * Never label a count with the wrong unit. */
export type StepUnit = 'issues' | 'sessions'

/** One routed outcome of a field-routed step (e.g. triage's classification):
 * how many items went each way. */
export interface StepRoute {
  outcome: string
  count: number
}

/** L0 — one step's throughput. `entered`/`done` are EVENT counts (rework counts
 * twice, so `done` CAN exceed `entered` — this is not a monotonic funnel);
 * `distinctEntered`/`distinctDone` are ITEM counts and legitimately disagree
 * with the event counts. `inFlight` is distinct items admitted and not yet
 * observed leaving. */
export interface OverviewStep {
  key: string
  label: string
  unit: StepUnit
  entered: number
  done: number
  skipped: number
  churn: number
  recentEntered: number
  recentDone: number
  inFlight: number
  distinctEntered: number
  distinctDone: number
  routed: StepRoute[]
}

/** One event name the fold could not map to a step, with how often it appeared —
 * a coverage signal for the view, not an error. */
export interface UnmappedEvent {
  event: string
  count: number
}

/** L0 — the whole pipeline. Timestamps are epoch SECONDS and may be null. */
export interface OverviewResponse {
  steps: OverviewStep[]
  totalEvents: number
  unparseable: number
  unmappedEvents: UnmappedEvent[]
  firstEventAt: number | null
  lastEventAt: number | null
  recentHours: number
}

/** L1 — one issue sitting in a step. Timestamps are epoch SECONDS and may be
 * null; `pr` may be null; every array may be empty. */
export interface StepItem {
  number: number
  title: string
  labels: string[]
  author: string
  assignees: string[]
  /** null when the local issue cache has no answer -- NOT the same as zero. */
  comments: number | null
  queuedAt: number | null
  dispatchedAt: number | null
  resumeCount: number
  slot: string
  previousSlots: string[]
  withdrawn: boolean
  needsHuman: boolean
  pr: number | null
  lastEvent: string
  lastEventAt: number | null
}

/** L1 — the items inside one step. */
export interface StepResponse {
  step: string
  count: number
  items: StepItem[]
}

/** L2 — one agent session that worked an item, with what it cost.
 *
 * `turns` is the usage ROW COUNT, which is the honest turn count: the usage
 * endpoint's contract is one row per turn, and the rows' own `turns` field is
 * structurally zero in real data, so the backend deliberately does not send it.
 * Timestamps are epoch SECONDS and may be null; every numeric field can be zero
 * (tokens and cost are always zero today), which is why `populatedColumns` exists
 * — render only the columns it names. */
export interface ItemSession {
  slot: string
  model: string
  agent: string
  surface: string
  current: boolean
  startedAt: number | null
  lastAt: number | null
  turns: number
  input: number
  output: number
  cacheCreate: number
  cacheRead: number
  cost: number
  credits: number
  durationMs: number
  contextUsed: number
  contextWindow: number
  lastPhase: string
  lastStopReason: string
}

/** L2 — the sessions that worked one item. `populatedColumns` names the numeric
 * columns that carry data; a column ABSENT from it must NOT be rendered, else
 * the table prints a column of zeros (tokens/cost are always zero today) beside
 * a real credit total. */
export interface ItemSessionsResponse {
  number: number
  count: number
  sessions: ItemSession[]
  populatedColumns: string[]
}

/** Read the string at `k` on `o`, or `fallback` (default '') when absent/wrong
 * type. The folds emit `_printable` strings, but a partial payload may omit one. */
function str(o: Record<string, unknown>, k: string, fallback = ''): string {
  const v = o[k]
  return typeof v === 'string' ? v : fallback
}

/** Read a finite number at `k`, or `fallback` (default 0). A null, string,
 * NaN or Infinity coerces to the fallback so one bad field cannot poison a total
 * the view renders as money or a count. */
function num(o: Record<string, unknown>, k: string, fallback = 0): number {
  const v = o[k]
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback
}

/** Read an epoch-seconds timestamp at `k` — a finite number, else null. Unlike
 * `num`, ABSENCE is meaningful here (an event with no recorded time), so it does
 * not collapse to 0. */
function ts(o: Record<string, unknown>, k: string): number | null {
  const v = o[k]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Read a nullable item/PR number at `k` — a finite number, else null. */
function numOrNull(o: Record<string, unknown>, k: string): number | null {
  const v = o[k]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Read the boolean at `k`, coercing anything non-boolean to false. */
function bool(o: Record<string, unknown>, k: string): boolean {
  return o[k] === true
}

/** Coerce an unknown into an array of strings, dropping non-string / empty
 * entries. A missing or non-array value yields []. */
function strList(v: unknown): string[] {
  if (!Array.isArray(v)) return []
  const out: string[] = []
  for (const e of v) if (typeof e === 'string' && e) out.push(e)
  return out
}

/** Narrow an unknown to a plain object, or null. */
function asObject(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

/** GET `path` and parse a JSON object body. A FAILURE THROWS -- transport,
 * non-2xx, non-JSON and non-object all raise.
 *
 * The three fold clients use this. A nullable reader is right for the older
 * Issue Radar seams, where an absent answer and an empty one mean the same thing to
 * the caller -- but it is wrong for these, because the views distinguish "nothing is
 * in this step" from "we could not ask". Returning an empty payload on a transport
 * failure made `isError` unreachable, which in turn made the views' error branches
 * dead code and put a confident "No pipeline activity yet" in front of a backend
 * outage. The error has to reach the query for the view to be able to tell the
 * truth.
 */
async function getObjectOrThrow(path: string): Promise<Record<string, unknown>> {
  const r = await fetch(path, { credentials: 'same-origin' })
  if (!r.ok) throw new Error(`request failed with status ${r.status}`)
  const body: unknown = await r.json()
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('response was not an object')
  }
  return body as Record<string, unknown>
}

function coerceOverviewStep(v: unknown): OverviewStep | null {
  const o = asObject(v)
  if (!o) return null
  const routed: StepRoute[] = []
  if (Array.isArray(o.routed)) {
    for (const e of o.routed) {
      const r = asObject(e)
      if (r) routed.push({ outcome: str(r, 'outcome'), count: num(r, 'count') })
    }
  }
  return {
    key: str(o, 'key'),
    label: str(o, 'label'),
    // The backend only ever emits 'issues' | 'sessions'; anything else is a
    // partial payload, so default to 'issues' rather than trusting a stray value.
    unit: o.unit === 'sessions' ? 'sessions' : 'issues',
    entered: num(o, 'entered'),
    done: num(o, 'done'),
    skipped: num(o, 'skipped'),
    churn: num(o, 'churn'),
    recentEntered: num(o, 'recentEntered'),
    recentDone: num(o, 'recentDone'),
    inFlight: num(o, 'inFlight'),
    distinctEntered: num(o, 'distinctEntered'),
    distinctDone: num(o, 'distinctDone'),
    routed,
  }
}

function coerceStepItem(v: unknown): StepItem | null {
  const o = asObject(v)
  if (!o) return null
  return {
    number: num(o, 'number'),
    title: str(o, 'title'),
    labels: strList(o.labels),
    author: str(o, 'author'),
    assignees: strList(o.assignees),
    // Preserved as null when the server sends null: `num()` would coerce it to 0,
    // which is the very claim the backend stopped making.
    comments: typeof o.comments === 'number' && Number.isFinite(o.comments) ? o.comments : null,
    queuedAt: ts(o, 'queuedAt'),
    dispatchedAt: ts(o, 'dispatchedAt'),
    resumeCount: num(o, 'resumeCount'),
    slot: str(o, 'slot'),
    previousSlots: strList(o.previousSlots),
    withdrawn: bool(o, 'withdrawn'),
    needsHuman: bool(o, 'needsHuman'),
    pr: numOrNull(o, 'pr'),
    lastEvent: str(o, 'lastEvent'),
    lastEventAt: ts(o, 'lastEventAt'),
  }
}

function coerceItemSession(v: unknown): ItemSession | null {
  const o = asObject(v)
  if (!o) return null
  return {
    slot: str(o, 'slot'),
    model: str(o, 'model'),
    agent: str(o, 'agent'),
    surface: str(o, 'surface'),
    current: bool(o, 'current'),
    startedAt: ts(o, 'startedAt'),
    lastAt: ts(o, 'lastAt'),
    turns: num(o, 'turns'),
    input: num(o, 'input'),
    output: num(o, 'output'),
    cacheCreate: num(o, 'cacheCreate'),
    cacheRead: num(o, 'cacheRead'),
    cost: num(o, 'cost'),
    credits: num(o, 'credits'),
    durationMs: num(o, 'durationMs'),
    contextUsed: num(o, 'contextUsed'),
    contextWindow: num(o, 'contextWindow'),
    lastPhase: str(o, 'lastPhase'),
    lastStopReason: str(o, 'lastStopReason'),
  }
}

/** The query params for a step request — owner/repo/step plus an optional limit. */
export interface StepQuery extends RepoRef {
  step: string
  limit?: number
}

/** The read clients for this app's own backend. All three are forward-tolerant
 * and NEVER throw — every degraded path returns a designed empty result. */
export const autoTriagePipelineFoldApi = {
  /**
   * L0 — the pipeline overview for the last `hours` (default: server's own
   * window). Empty result on any failure: no steps, zero counts, null bounds.
   */
  overview: async (hours?: number): Promise<OverviewResponse> => {
    const empty = (): OverviewResponse => ({
      steps: [],
      totalEvents: 0,
      unparseable: 0,
      unmappedEvents: [],
      firstEventAt: null,
      lastEventAt: null,
      recentHours: typeof hours === 'number' && Number.isFinite(hours) ? hours : 0,
    })
    const q = new URLSearchParams()
    if (typeof hours === 'number' && Number.isFinite(hours)) q.set('hours', String(Math.trunc(hours)))
    const suffix = q.toString() ? `?${q.toString()}` : ''
    const o = await getObjectOrThrow(`${ATP_API}/overview${suffix}`)
    const steps: OverviewStep[] = []
    if (Array.isArray(o.steps)) {
      for (const s of o.steps) {
        const step = coerceOverviewStep(s)
        if (step) steps.push(step)
      }
    }
    const unmappedEvents: UnmappedEvent[] = []
    if (Array.isArray(o.unmappedEvents)) {
      for (const e of o.unmappedEvents) {
        const r = asObject(e)
        if (r) unmappedEvents.push({ event: str(r, 'event'), count: num(r, 'count') })
      }
    }
    return {
      steps,
      totalEvents: num(o, 'totalEvents'),
      unparseable: num(o, 'unparseable'),
      unmappedEvents,
      firstEventAt: ts(o, 'firstEventAt'),
      lastEventAt: ts(o, 'lastEventAt'),
      recentHours: num(o, 'recentHours', empty().recentHours),
    }
  },

  /**
   * L1 — the items sitting in one step. `step`/`owner`/`repo` are required;
   * `limit` is optional. Empty result (`items: []`, `count: 0`, echoing the
   * requested step) on any failure.
   */
  step: async (query: StepQuery): Promise<StepResponse> => {
    const q = new URLSearchParams({ step: query.step, owner: query.owner, repo: query.repo })
    if (query.provider) q.set('provider', query.provider)
    if (query.host) q.set('host', query.host)
    if (typeof query.limit === 'number' && Number.isFinite(query.limit)) {
      q.set('limit', String(Math.trunc(query.limit)))
    }
    const o = await getObjectOrThrow(`${ATP_API}/step?${q.toString()}`)
    const items: StepItem[] = []
    if (Array.isArray(o.items)) {
      for (const it of o.items) {
        const row = coerceStepItem(it)
        if (row) items.push(row)
      }
    }
    return { step: str(o, 'step', query.step), count: num(o, 'count'), items }
  },

  /**
   * L2 — the sessions that worked one item. Empty result (`sessions: []`,
   * `count: 0`, `populatedColumns: []`, echoing the requested number) on any
   * failure.
   */
  itemSessions: async (number: number): Promise<ItemSessionsResponse> => {
    const q = new URLSearchParams({ number: String(Math.trunc(number)) })
    const o = await getObjectOrThrow(`${ATP_API}/item/sessions?${q.toString()}`)
    const sessions: ItemSession[] = []
    if (Array.isArray(o.sessions)) {
      for (const s of o.sessions) {
        const row = coerceItemSession(s)
        if (row) sessions.push(row)
      }
    }
    return {
      number: num(o, 'number', number),
      count: num(o, 'count'),
      sessions,
      populatedColumns: strList(o.populatedColumns),
    }
  },
}
