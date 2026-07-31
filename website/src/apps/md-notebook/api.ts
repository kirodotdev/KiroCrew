/**
 * API client for the Notes app.
 *
 * Two different servers are involved:
 *  - `API_BASE` is the app's own backend, reached through the gateway proxy.
 *  - the Knowledge calls go to the DASHBOARD's own API, because registering a
 *    folder needs the user's session. `/api/knowledge` is declared in the
 *    manifest's `permissions.api` for exactly that reason.
 */
import { i18nT } from '../../i18n/t'
import { API_BASE } from './constants'
import { vaultContentPath } from './utils'
import type { Note, NoteDoc, SearchHit, SyncResult, Vault } from './types'

/** An API failure that carries the response payload callers need. */
export class ApiError extends Error {
  status: number
  body: Record<string, unknown>
  /** The backend process predates this UI bundle. */
  staleBackend: boolean

  constructor(message: string, status: number, body: Record<string, unknown>, stale = false) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
    this.staleBackend = stale
  }
}

/** Catalog key for the message shown when the running backend is older than this page. */
const STALE_BACKEND_KEY = 'apps.mdNotebook.banner.staleBackendRoute'

async function mdnbCall<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(API_BASE + path, {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const json = (await res.json().catch(() => ({}))) as Record<string, unknown>
  if (!res.ok) {
    const raw = typeof json.error === 'string' ? json.error : res.statusText
    // A "no route" reply means the gateway kept an older backend process alive
    // across a UI reload. Translating it here means ANY endpoint added later
    // fails with an actionable message, with no list to keep in sync.
    const stale = /^no route: /.test(raw)
    throw new ApiError(stale ? i18nT(STALE_BACKEND_KEY) : raw, res.status, json, stale)
  }
  return json as T
}

/** Append the active vault to a vault-scoped path. */
function mdnbVaultQuery(path: string, vault: string | null): string {
  if (!vault) return path
  // URLSearchParams (an excluded machine-key callee) builds the query so there
  // is no `vault=` copy literal, and it handles encoding.
  const sep = path.includes('?') ? '&' : '?'
  return path + sep + new URLSearchParams({ vault }).toString()
}

export const notesApi = {
  health: () => mdnbCall<{ ok: boolean; features: string[] }>('GET', '/health'),

  listVaults: () =>
    mdnbCall<{ vaults: Vault[]; hasPat: boolean; hasGhAuth: boolean }>('GET', '/vaults'),

  cloneVault: (payload: {
    url: string
    pat?: string
    branch?: string
    subfolder?: string
    knowledge?: boolean
  }) => mdnbCall<{ vault: Vault }>('POST', '/vaults', payload),

  attachVault: (payload: { path: string; subfolder?: string; knowledge?: boolean }) =>
    mdnbCall<{ vault: Vault }>('POST', '/vaults/attach', payload),

  forgetVault: (id: string) =>
    mdnbCall<{ ok: boolean; localPath: string }>('DELETE', `/vaults?vault=${encodeURIComponent(id)}`),

  setVaultKnowledge: (vault: string, knowledge: boolean, sourceId?: string) =>
    mdnbCall<{ vault: Vault }>('PUT', '/vaults/knowledge', { vault, knowledge, sourceId }),

  setPat: (pat: string) =>
    mdnbCall<{ hasPat: boolean; hasGhAuth: boolean }>('PUT', '/pat', { pat }),

  pickFolder: () => mdnbCall<{ path: string | null; cancelled: boolean }>('POST', '/pick-folder'),

  listNotes: (vault: string | null) => mdnbCall<{ notes: Note[] }>('GET', mdnbVaultQuery('/notes', vault)),

  readNote: (vault: string | null, path: string) =>
    mdnbCall<NoteDoc>('GET', mdnbVaultQuery(`/note?path=${encodeURIComponent(path)}`, vault)),

  saveNote: (vault: string | null, path: string, content: string, baseMtime?: number) =>
    mdnbCall<{ ok: boolean; mtime: number }>('PUT', mdnbVaultQuery('/note', vault), {
      path,
      content,
      baseMtime,
    }),

  deleteNote: (vault: string | null, path: string) =>
    mdnbCall<{ ok: boolean }>('DELETE', mdnbVaultQuery(`/note?path=${encodeURIComponent(path)}`, vault)),

  newNote: (vault: string | null, folder?: string) =>
    mdnbCall<{ path: string }>('POST', mdnbVaultQuery('/note/new', vault), { folder }),

  moveNote: (vault: string | null, from: string, to: string) =>
    mdnbCall<{ ok: boolean; path: string }>('POST', mdnbVaultQuery('/note/move', vault), { from, to }),

  sync: (vault: string | null) => mdnbCall<{ result: SyncResult }>('POST', mdnbVaultQuery('/sync', vault)),

  search: (vault: string | null, q: string) =>
    mdnbCall<{ results: SearchHit[] }>('GET', mdnbVaultQuery(`/search?q=${encodeURIComponent(q)}`, vault)),

  changes: (vault: string | null, since: number) =>
    mdnbCall<{ rev: number; changed: string[]; watching: boolean }>(
      'GET',
      mdnbVaultQuery(`/changes?since=${since}`, vault),
    ),
}

// ---------------------------------------------------------------------------
// Kiro Crew Knowledge library (host API, not the app's backend)
// ---------------------------------------------------------------------------

/**
 * Register the vault folder as a Knowledge source and confirm it so ingestion
 * starts. A 409 means the folder is already registered — adopt that id rather
 * than failing, so a re-enable after a partial failure recovers cleanly.
 */
export async function knowledgeRegister(
  vault: Vault,
): Promise<{ sourceId: string; fileCount?: number }> {
  const res = await fetch('/api/knowledge/sources', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: vault.name,
      source_type: 'obsidian_vault',
      uri: vaultContentPath(vault),
      properties: {},
    }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    id?: string
    error?: string
    file_count?: number
  }
  if (!res.ok && res.status !== 409) {
    throw new Error(body.error || `knowledge add failed (${res.status})`)
  }
  if (!body.id) throw new Error(body.error || 'knowledge add returned no source id')
  // Folder sources land as pending_confirmation; confirming begins the scan.
  // A swallowed failure here left the vault recorded as indexed while its source
  // sat pending, so the notes were never actually searchable.
  const confirm = await fetch(`/api/knowledge/sources/${encodeURIComponent(body.id)}/confirm`, {
    method: 'POST',
    credentials: 'same-origin',
  })
  if (!confirm.ok) {
    const detail = (await confirm.json().catch(() => ({}))) as { error?: string }
    // The source exists but is stuck pending. Remove it rather than leaving an
    // orphan in the Knowledge library that indexes nothing.
    await knowledgeUnregister(body.id).catch(() => undefined)
    throw new Error(detail.error || `knowledge confirm failed (${confirm.status})`)
  }
  return { sourceId: body.id, fileCount: body.file_count }
}

/** Remove a vault's Knowledge source. Already-gone is not an error. */
export async function knowledgeUnregister(sourceId?: string | null): Promise<void> {
  if (!sourceId) return
  const res = await fetch(`/api/knowledge/sources/${encodeURIComponent(sourceId)}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  })
  if (!res.ok && res.status !== 404) {
    const body = (await res.json().catch(() => ({}))) as { error?: string }
    throw new Error(body.error || `knowledge remove failed (${res.status})`)
  }
}
