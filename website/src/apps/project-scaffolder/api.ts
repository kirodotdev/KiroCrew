/**
 * Typed client for the two host folder-scaffolding endpoints.
 *
 * App-local rather than a pair of methods on the shared `api/client.ts` for one
 * reason: this surface has to read the `code` out of a 400 body, and the shared
 * client's `j()` collapses a failure into an `Error` whose message is the
 * advisory prose. `code` is the contract the UI branches on (a stale selection
 * needs a re-scan prompt, an unusable root needs an inline field error), so it
 * has to survive the throw.
 *
 * There is no scan or create logic here, deliberately. The server re-derives the
 * selection from its own fresh scan, so a candidate path is only ever a pick from
 * what the server offered; duplicating any part of that here would create a
 * second answer to "what does this tree contain".
 */

/** Server response `status` values. `empty` is a successful scan of a tree with nothing in it. */
export const STATUS_EMPTY = 'empty'

/** `code` on the 400 that means the preview no longer matches the tree on disk. */
export const CODE_SELECTION_STALE = 'folder_scaffold_selection_stale'

/** Confidence tier the scanner assigned. `auto` starts ticked, `offered` does not. */
export type Tier = 'auto' | 'offered'

export interface Candidate {
  /** Absolute directory path. Also the identifier a scaffold request selects with. */
  path: string
  name: string
  /** Parent candidate's path, or null when the candidate hangs off the scan root. */
  parent_path: string | null
  tier: Tier
  signals: string[]
  /** A folder is already bound to this directory. Reported, never re-created. */
  existing: boolean
  /** The server's default tick state. */
  selected: boolean
}

export interface ScanResult {
  root: string
  root_existing: boolean
  status: string
  candidates: Candidate[]
  warnings: string[]
}

export interface CreatedFolder {
  path: string
  folder_id: string
  name: string
}

export interface FailedFolder {
  path: string
  error: string
  code: string
}

export interface ScaffoldResult {
  root: string
  created: CreatedFolder[]
  skipped_existing: string[]
  failed: FailedFolder[]
  warnings: string[]
}

/**
 * A refused request, carrying the two halves of the server's 400 body.
 *
 * `message` is the server's own `error` prose and is rendered verbatim: it is
 * the same sentence creating a folder by hand would have produced, so
 * re-wording it here would make the two surfaces disagree about the same
 * refusal. `code` is what the UI branches on.
 */
export class ScaffoldApiError extends Error {
  readonly status: number
  readonly code: string
  /** Selected paths the current scan no longer offers (stale-selection refusals only). */
  readonly unknown: string[]
  constructor(status: number, code: string, message: string, unknownPaths: string[] = []) {
    super(message)
    this.name = 'ScaffoldApiError'
    this.status = status
    this.code = code
    this.unknown = unknownPaths
  }
}

async function toError(r: Response): Promise<ScaffoldApiError> {
  const text = await r.text().catch(() => '')
  let code = ''
  let message = text || `HTTP ${r.status}`
  let unknownPaths: string[] = []
  if (text) {
    try {
      const parsed = JSON.parse(text) as { error?: unknown; code?: unknown; unknown?: unknown }
      if (typeof parsed.code === 'string') code = parsed.code
      if (typeof parsed.error === 'string') message = parsed.error
      if (Array.isArray(parsed.unknown)) {
        unknownPaths = parsed.unknown.filter((p): p is string => typeof p === 'string')
      }
    } catch {
      /* not JSON — keep the raw body as the message */
    }
  }
  return new ScaffoldApiError(r.status, code, message, unknownPaths)
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw await toError(r)
  return (await r.json()) as T
}

/** Preview the folders a project tree would produce. Creates nothing. */
export function scanProject(root: string): Promise<ScanResult> {
  return postJson<ScanResult>('/api/project-scaffold/scan', { root })
}

/**
 * Create the scan root's folder plus the selected candidates beneath it.
 *
 * `selected` carries exactly the paths ticked at confirmation time. The server
 * re-scans and refuses the whole call when any of them is no longer a candidate,
 * so an empty list is a real answer (create the root folder only) rather than a
 * missing field.
 */
export function scaffoldProject(root: string, selected: string[]): Promise<ScaffoldResult> {
  return postJson<ScaffoldResult>('/api/project-scaffold/create', { root, selected })
}
