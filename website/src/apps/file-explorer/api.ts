import { API_BASE } from './constants'
import { toApiError } from '../../api/apiError'
import type { TreeEntry, FileMeta, SearchResult, OfficeExtract, WriteResult } from './types'

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin' })
  if (!r.ok) {
    throw await toApiError(r)
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
   * concurrent writer is never silently clobbered.
   *
   * `baseToken` is an OPAQUE STRING, never a number. It carries the file's
   * nanosecond mtime, which exceeds JavaScript's safe-integer range
   * (2^53-1): parsed as a JSON number it silently rounds, the backend's
   * exact-match guard then never matches, and EVERY save fails 409. It is
   * therefore passed through as text end to end and never arithmetic. */
  write: async (path: string, content: string, baseMtime?: number, baseToken?: string) => {
    const q = new URLSearchParams({ path })
    if (baseMtime) q.set('base_mtime', String(baseMtime))
    if (baseToken != null) q.set('base_token', baseToken)
    const r = await fetch(`${API_BASE}/write?${q.toString()}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'text/markdown' },
      body: content,
    })
    // Built BEFORE the body is consumed: toApiError reads the response itself,
    // and it unwraps {"error": …} into the message while keeping the status, so
    // a 409 still reaches the editor's conflict branch.
    if (!r.ok) {
      throw await toApiError(r)
    }
    return await r.json() as WriteResult
  },
}
