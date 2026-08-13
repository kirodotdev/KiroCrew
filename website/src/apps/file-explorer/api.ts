import { API_BASE } from './constants'
import type { TreeEntry, FileMeta, SearchResult } from './types'

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
}
