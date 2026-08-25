import { API_BASE } from './constants'
import type { TreeEntry, FileMeta, SearchResult, OfficeExtract, WriteResult } from './types'

/** An HTTP failure that keeps the status code, so callers can tell a 409
 * write conflict apart from any other save error. */
export class FileExplorerApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin' })
  if (!r.ok) {
    const body = await r.text().catch(() => '')
    throw new Error(body || `HTTP ${r.status}`)
  }
  return r.json()
}

export const fileExplorerApi = {
  health: () => get<{ allowedRoots: string[]; home?: string }>(`${API_BASE}/health`),

  tree: (path: string, depth = 1) =>
    get<{ entries: TreeEntry[] }>(`${API_BASE}/tree?path=${encodeURIComponent(path)}&depth=${depth}`),

  read: (path: string, maxBytes?: number) => {
    const q = new URLSearchParams({ path })
    if (maxBytes) q.set('max_bytes', String(maxBytes))
    return get<FileMeta>(`${API_BASE}/read?${q.toString()}`)
  },

  search: (path: string, q: string, include = '', exclude = '') => {
    const params = new URLSearchParams({ path, q })
    if (include) params.set('include', include)
    if (exclude) params.set('exclude', exclude)
    return get<{ results: SearchResult[]; engine?: string; truncated?: boolean }>(`${API_BASE}/search?${params.toString()}`)
  },

  gitStatus: (path: string) =>
    get<{ repoRoot: string; branch?: string; statuses: Record<string, string> } | null>(`${API_BASE}/git-status?path=${encodeURIComponent(path)}`),

  resolve: (path: string) =>
    get<{ exists: boolean; type: string }>(`${API_BASE}/resolve?path=${encodeURIComponent(path)}`),

  complete: (path: string, kind = 'dir', limit = 30) => {
    const q = new URLSearchParams({ path, kind, limit: String(limit) })
    return get<{ entries: TreeEntry[] }>(`${API_BASE}/complete?${q.toString()}`)
  },

  /** URL that streams a file's bytes with its real Content-Type — used as an
   * iframe/img/audio/video `src`, and (with `download`) as a download href. */
  rawUrl: (path: string, download = false) => {
    const q = new URLSearchParams({ path })
    if (download) q.set('download', '1')
    return `${API_BASE}/raw?${q.toString()}`
  },

  /** Structured content of an Office document (docx/xlsx/pptx). */
  extract: (path: string) =>
    get<OfficeExtract>(`${API_BASE}/extract?path=${encodeURIComponent(path)}`),

  /** URL streaming one embedded media member (slide images) of a document. */
  extractMemberUrl: (path: string, member: string) => {
    const q = new URLSearchParams({ path, member })
    return `${API_BASE}/extract?${q.toString()}`
  },

  /** Save markdown content. `baseMtime` is the mtime the editor loaded; the
   * backend answers 409 when the file changed on disk since then, so a
   * concurrent writer is never silently clobbered. */
  write: async (path: string, content: string, baseMtime?: number, baseToken?: number) => {
    const q = new URLSearchParams({ path })
    if (baseMtime) q.set('base_mtime', String(baseMtime))
    if (baseToken != null) q.set('base_token', String(baseToken))
    const r = await fetch(`${API_BASE}/write?${q.toString()}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'text/markdown' },
      body: content,
    })
    const body = await r.json().catch(() => ({}))
    if (!r.ok) {
      throw new FileExplorerApiError(
        (body as { error?: string }).error || `HTTP ${r.status}`, r.status,
      )
    }
    return body as WriteResult
  },
}
