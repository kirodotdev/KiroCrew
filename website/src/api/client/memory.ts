/**
 * Agent memory under /api/memory: memory files and settings, stores, retired
 * rows and backups, carve/recall and record editing, member memory pages and
 * seeding, vector semantic and episodic memory with embeddings, the graph and
 * consolidation, learned lessons, and knowledge search for context.
 *
 * `deleteLesson` is defined in `api/client.ts`, which learn-cron-dashboard.md
 * names as its home.
 */

import { relocateRequestUrl } from '../../lib/dashboardRuntime'
import type { MemoryCarveEntry, MemoryStoreSummary, RetiredMemory, MemoryBackup } from '../../types'
import type { MemoryRecordQuery, MemoryRecord, MemoryRecordSelection, MemoryEditOperation, MemoryEditPreview, MemoryRecordRef, MemoryRecordRevision } from '../../types/memoryEditing'
import type { ClientTransport } from './transport'

/**
 * The optional `store=` parameter on memory content routes.
 * Embedding configuration is installation-wide; legacy Markdown migration
 * accepts only Global V1, and automatic episode promotion refuses private V2.
 *
 * Returns the EMPTY string when the caller named no store, and that absence is
 * load-bearing: the gateway reads a missing parameter as "the global
 * store" and applies the owner gate plus the unknown-name 404 only to
 * a parameter that is actually present. Sending `store=` for an unnamed store
 * would therefore turn a request that works for anyone into an owner-only one.
 *
 * `sep` is `'&'` for a URL that already carries a query string.
 *
 * The two separators are spelled out rather than interpolated from `sep` so that
 * each literal reaching the i18n linter carries its own leading `?`/`&`. That is
 * what `eslint.i18n.config.js` matches URL-query fragments on
 * (`^[?&][a-z_]+=$`); interpolating the separator leaves the linter a bare
 * `store=`, which reads as a user-facing string it should be asking about.
 * Written here rather than by widening that pattern, since a pattern that admits
 * a leading-separator-less fragment stops catching real strings elsewhere.
 */
const memoryStoreQuery = (store?: string, sep: '?' | '&' = '?'): string => {
  if (!store) return ''
  const value = encodeURIComponent(store)
  return sep === '?' ? `?store=${value}` : `&store=${value}`
}

/** What one `GET /api/memory/carve` read asks for. */
export interface MemoryCarveQuery {
  store?: string
  /** A `memory_schema.GROUPABLE_COLUMNS` axis. Set it to ask for counts instead
   *  of rows; the response then carries `counts` rather than `entries`. */
  countBy?: string
  /** One `memory_schema.ALL_KINDS` row type, or `''` for every kind. */
  kind?: string
  /** Facet name -> exact value, ANDed together. A value of `''` is MEANINGFUL —
   *  it selects the rows no writer attributed on that axis — so the mapping is
   *  transmitted key by key rather than filtered on truthiness. */
  facets?: Record<string, string>
  limit?: number
  offset?: number
}

/** Either shape `GET /api/memory/carve` answers with, discriminated by which
 *  field is present: `counts` when the read named a `count_by` axis, `entries`
 *  otherwise. */
export interface MemoryCarveResult {
  /** The store the gateway resolved, `''` for the global one. */
  store?: string
  entries?: MemoryCarveEntry[]
  counts?: Record<string, number>
}

/**
 * Query string for a `/api/memory/*` route, from the parameters a caller set.
 *
 * `facets` is handled apart from the scalars because an EMPTY facet value is
 * meaningful: `?crew=` selects the rows no writer attributed on that axis, which
 * is a different question from omitting `crew` altogether. So facets reach the
 * wire verbatim while an unset scalar is dropped.
 */
const memoryQuery = (q: MemoryCarveQuery): string => {
  const p = new URLSearchParams()
  if (q.store) p.set('store', q.store)
  if (q.countBy) p.set('count_by', q.countBy)
  if (q.kind) p.set('kind', q.kind)
  for (const [name, value] of Object.entries(q.facets ?? {})) p.set(name, value)
  if (q.limit !== undefined) p.set('limit', String(q.limit))
  if (q.offset !== undefined) p.set('offset', String(q.offset))
  const s = p.toString()
  return s ? `?${s}` : ''
}

export function createMemoryEndpoints({ get, post, put, del, j }: ClientTransport) {
  const memoryAndVectors = {
    // Memory
    //
    // `store` is optional on every route here and threads through to
    // `?store=<name>`. Omitted, the gateway serves the caller's own binding, which
    // is what these calls did before the parameter existed; named, the request is
    // owner-gated and an undeclared name answers 404 `unknown_memory_store`. See
    // `memoryStoreQuery`. `/api/memory/settings` is deliberately NOT in the set:
    // the consolidation cadence is one install-wide setting, not a per-store one.
    memoryPreferences: (store?: string) => fetch(relocateRequestUrl('/api/memory/preferences' + memoryStoreQuery(store))).then(j) as Promise<{ content?: string; content_redacted?: boolean }>,
    saveMemoryPreferences: (content: string, store?: string) => put('/api/memory/preferences' + memoryStoreQuery(store), { content }),
    memoryProjects: (store?: string) => fetch(relocateRequestUrl('/api/memory/projects' + memoryStoreQuery(store))).then(j) as Promise<{ content?: string; content_redacted?: boolean }>,
    saveMemoryProjects: (content: string, store?: string) => put('/api/memory/projects' + memoryStoreQuery(store), { content }),
    memoryHistory: (store?: string) => fetch(relocateRequestUrl('/api/memory/history' + memoryStoreQuery(store))).then(j) as Promise<{ content?: string; content_redacted?: boolean }>,
    saveMemoryHistory: (content: string, store?: string) => put('/api/memory/history' + memoryStoreQuery(store), { content }),
    memorySettings: () => fetch(relocateRequestUrl('/api/memory/settings')).then(j),
    saveMemorySettings: (s: {history_idle_hours?: number; history_max_days?: number}) => put('/api/memory/settings', s),
    /** Every declared store with its lineage, row counts and backup state.
     *
     *  Owner-gated unconditionally — it enumerates every silo — and takes no
     *  `store` of its own. Order is the gateway's (`default` first, then sorted);
     *  do not re-sort it in a caller. */
    /** Every declared store, plus `active`: the one this caller already reads with
     *  no `store=` on the wire. The picker needs `active` to leave the parameter off
     *  for that store, since sending it would take the owner gate for a read that
     *  needs none. */
    memoryStores: () =>
      fetch(relocateRequestUrl('/api/memory/stores')).then(j) as Promise<{
        stores: MemoryStoreSummary[]
        active: string
      }>,
    memoryRetired: (store?: string, limit?: number, offset = 0) =>
      fetch(relocateRequestUrl('/api/memory/retired' + memoryQuery({ store, limit, ...(offset ? { offset } : {}) }))).then(j) as Promise<{ retired: RetiredMemory[] }>,
    memoryRestoreRetired: (id: string, store?: string) =>
      post('/api/memory/retired/restore', { id, ...(store ? { store } : {}) }).then(j) as Promise<{ ok: boolean }>,
    memoryBackups: (store?: string) =>
      fetch(relocateRequestUrl('/api/memory/backups' + memoryStoreQuery(store))).then(j) as Promise<{
        backups: MemoryBackup[]; pending?: boolean; restart_required?: boolean
        pending_restore?: { backup_name: string; staged_at: string } | null
        activation_failed?: boolean
        restore_error?: string
        recovery?: { journal: string; staged_copies: string[]; previous_copies: string[]; instruction: string }
      }>,
    memoryBackupNow: (store?: string) =>
      post('/api/memory/backup', store ? { store } : {}).then(j) as Promise<{
        backed_up: number; skipped: number; pruned: number; failed: number
      }>,
    /** Put one backup back in place. `name` is the handle `memoryBackups` returned,
     *  never a path — the gateway resolves it inside the store's own backup
     *  directory. `superseded` names the file the restore moved aside. */
    memoryRestoreBackup: (name: string, store?: string) =>
      post('/api/memory/restore', { name, ...(store ? { store } : {}) }).then(j) as Promise<{
        ok: boolean; superseded?: string; pending?: boolean; restart_required?: boolean
      }>,
    cancelMemberMemoryRestore: (store: string) =>
      post('/api/memory/restore/cancel', { store }).then(j) as Promise<{
        ok: boolean; cancelled: boolean; pending: false; restart_required: boolean; pending_restore: null
        activation_failed?: boolean; restore_error?: string
      }>,
    /** One carve read: `counts` when `countBy` is set, `entries` otherwise. Answers
     *  409 `facets_unsupported` on the v1 lineage, whose rows have no facet
     *  columns — a refusal a caller must render as such, never as no rows. */
    memoryCarve: (q: MemoryCarveQuery = {}) =>
      fetch(relocateRequestUrl('/api/memory/carve' + memoryQuery(q))).then(j) as Promise<MemoryCarveResult>,
    memoryRecall: (query: string, store: string) =>
      fetch(relocateRequestUrl('/api/memory/recall?q=' + encodeURIComponent(query) + memoryStoreQuery(store, '&'))).then(j),
    memoryRecords: (store: string, query: MemoryRecordQuery, offset = 0, limit = 50) =>
      fetch(relocateRequestUrl('/api/memory/records?' + new URLSearchParams({ store: store || 'default', q: query.q, kind: query.kind, offset: String(offset), limit: String(limit) }))).then(j) as Promise<{ entries: MemoryRecord[]; total: number; has_more: boolean }>,
    memoryEditPreview: (store: string, selection: MemoryRecordSelection, operation: MemoryEditOperation) =>
      post('/api/memory/bulk/preview', { store: store || 'default', selection, operation }).then(j) as Promise<MemoryEditPreview>,
    memoryEditPreviewPage: (store: string, previewId: string, offset: number) =>
      post('/api/memory/bulk/preview', { store: store || 'default', preview_id: previewId, offset }).then(j) as Promise<MemoryEditPreview>,
    memoryRecordsRefresh: (store: string, items: MemoryRecordRef[]) =>
      post('/api/memory/records/refresh', { store: store || 'default', items }).then(j) as Promise<{ entries: MemoryRecord[]; missing: MemoryRecordRef[] }>,
    memoryQuerySelectionRefresh: (store: string, selection: Extract<MemoryRecordSelection, { query: MemoryRecordQuery }>) =>
      post('/api/memory/records/refresh', { store: store || 'default', selection }).then(j) as Promise<{ matched_count: number }>,
    memoryRecordHistory: (store: string, record: MemoryRecordRef, limit = 25, offset = 0) =>
      fetch(relocateRequestUrl('/api/memory/records/history?' + new URLSearchParams({ store: store || 'default', kind: record.kind, id: record.id, limit: String(limit), ...(offset ? { offset: String(offset) } : {}) }))).then(j) as Promise<{ entries: MemoryRecordRevision[]; has_more: boolean; current_revision: number }>,
    memoryEditApply: (store: string, previewId: string) =>
      post('/api/memory/bulk/apply', { store: store || 'default', preview_id: previewId }).then(j) as Promise<{ ok: true; changed_count: number }>,
    memberMemoryPage: (store: string, kind: 'semantic' | 'episodic', offset: number, query = '') =>
      fetch(relocateRequestUrl('/api/memory/' + kind + '?store=' + encodeURIComponent(store) + '&limit=100&offset=' + offset + (query ? '&q=' + encodeURIComponent(query) : ''))).then(j),
    memorySeed: (sourceStore: string, store: string, items: { kind: 'fact' | 'directive' | 'episode'; id: string }[]) =>
      post('/api/memory/seed', { source_store: sourceStore, store, items }).then(j) as Promise<{
        partial?: boolean
        results: { outcome: 'imported' | 'existing' | 'rejected' | 'unconfirmed' | 'not_attempted'; id?: string; reason?: string }[]
      }>,
    // Vector memory
    vectorSemantic: (store?: string) => fetch(relocateRequestUrl('/api/memory/semantic' + memoryStoreQuery(store))).then(j),
    vectorSemanticWrite: (key: string, value: unknown, store?: string) => put('/api/memory/semantic' + memoryStoreQuery(store), { key, value, source: 'user_explicit' }).then(j),
    vectorSemanticDelete: (key: string, store?: string) => del('/api/memory/semantic/' + encodeURIComponent(key) + memoryStoreQuery(store)),
    vectorEpisodic: (limit = 50, offset = 0, tags?: string, store?: string) => fetch(relocateRequestUrl('/api/memory/episodic?limit=' + limit + '&offset=' + offset + (tags ? '&tags=' + encodeURIComponent(tags) : '') + memoryStoreQuery(store, '&'))).then(j),
    vectorEpisodicSearch: (q: string, tags?: string, store?: string) => fetch(relocateRequestUrl('/api/memory/episodic/search?q=' + encodeURIComponent(q) + (tags ? '&tags=' + encodeURIComponent(tags) : '') + memoryStoreQuery(store, '&'))).then(j),
    vectorEpisodicDelete: (id: string, store?: string) => del('/api/memory/episodic/' + encodeURIComponent(id) + memoryStoreQuery(store)),
    vectorStats: (store?: string) => fetch(relocateRequestUrl('/api/memory/stats' + memoryStoreQuery(store))).then(j),
    vectorEvents: (limit = 50, offset = 0, store?: string) => fetch(relocateRequestUrl('/api/memory/events?limit=' + limit + '&offset=' + offset + memoryStoreQuery(store, '&'))).then(j),
    vectorEmbeddingStatus: () => fetch(relocateRequestUrl('/api/memory/embedding-status')).then(j),
    vectorEnableEmbeddings: () => post('/api/memory/enable-embeddings').then(j),
    vectorValidateEmbedModel: (path: string) =>
      post('/api/memory/embedding-model', { path, validate_only: true }).then(j),
    vectorApplyEmbedModel: (path: string) =>
      post('/api/memory/embedding-model', { path }).then(j),
    vectorDisableEmbeddings: () => post('/api/memory/disable-embeddings').then(j),
    vectorImport: (data: object) => post('/api/memory/import', data).then(j),
    vectorContextPreview: (query?: string) => fetch(relocateRequestUrl('/api/memory/context-preview' + (query ? '?q=' + encodeURIComponent(query) : ''))).then(j),
    memoryGraph: () => fetch(relocateRequestUrl('/api/memory/graph')).then(j),
    consolidateMemory: (key: string, includeHistory: boolean) => post('/api/memory/consolidate', { key, include_history: includeHistory }).then(j),
  }

  const lessons = {
    // Lessons
    lessons: () => fetch(relocateRequestUrl('/api/lessons')).then(j),
    createLesson: (rule: string, category: string) =>
      post('/api/lessons', { rule, category }).then(j) as Promise<{
        ok: boolean
        outcome: 'inserted' | 'enriched' | 'unchanged' | 'deduped' | 'refused'
        reason: string
      }>,
  }

  const knowledge = {
    // Knowledge
    knowledgeSearch: (q: string) => get(`/api/knowledge/search-for-context?q=${encodeURIComponent(q)}`).then(j),
  }

  return { memoryAndVectors, lessons, knowledge }
}
