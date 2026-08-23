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
