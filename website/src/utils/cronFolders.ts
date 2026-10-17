import type { CronJob } from '../types'

/**
 * A cron folder as returned by the backend.
 */
export interface CronFolder {
  id: string
  name: string
  order: number
}

/**
 * Group jobs by folder_id. Returns an ordered array of { folder, jobs } plus
 * an ungrouped bucket (folder = null). Folders with no matching jobs are
 * omitted when `omitEmpty` is true.
 */
export interface CronFolderGroup<T extends CronJob = CronJob> {
  folder: CronFolder | null
  jobs: T[]
}

/** Generic over the job type so callers that decorate jobs (e.g. SchedulePage's
 *  `sanitizedJobs`, which adds `safeMessage`) keep the decorated type on the
 *  grouped output instead of having it erased back to bare `CronJob`. */
export function groupJobsByFolder<T extends CronJob>(
  jobs: T[],
  folders: CronFolder[],
  opts?: { omitEmpty?: boolean }
): CronFolderGroup<T>[] {
  const folderSet = new Set(folders.map(f => f.id))
  const folderMap = new Map<string, T[]>()
  const ungrouped: T[] = []

  for (const j of jobs) {
    const fid = (j as T & { folder_id?: string }).folder_id
    if (fid && folderSet.has(fid)) {
      const bucket = folderMap.get(fid)
      if (bucket) bucket.push(j)
      else folderMap.set(fid, [j])
    } else {
      ungrouped.push(j)
    }
  }

  const sorted = [...folders].sort((a, b) => a.order - b.order)
  const groups: CronFolderGroup<T>[] = []

  for (const f of sorted) {
    const fJobs = folderMap.get(f.id) || []
    if (opts?.omitEmpty && fJobs.length === 0) continue
    groups.push({ folder: f, jobs: fJobs })
  }

  // Ungrouped always trails
  if (ungrouped.length > 0 || groups.length > 0) {
    groups.push({ folder: null, jobs: ungrouped })
  }

  return groups
}

const COLLAPSED_KEY = 'kc-cron-folders-collapsed'

export function loadCollapsedFolders(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    if (raw) return new Set(JSON.parse(raw))
  } catch { /* noop */ }
  return new Set()
}

export function saveCollapsedFolders(ids: Set<string>): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...ids]))
  } catch { /* noop */ }
}
