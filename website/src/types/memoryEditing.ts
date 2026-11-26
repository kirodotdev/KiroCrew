export type MemoryRecordKind = 'fact' | 'directive' | 'episode'
/** The facet groups `GET /api/memory/records` filters on (`memory_record_facets.FACET_GROUPS`). */
export type MemoryRecordFacetGroup = 'category' | 'applies' | 'scope' | 'source' | 'surface' | 'crew' | 'tag'
/** Facet filters plus the confidence range; part of the query, so "select all
 *  matching" reaches exactly the filtered rows. */
export type MemoryRecordFilters = Partial<Record<MemoryRecordFacetGroup, string[]>> & { conf_min?: number; conf_max?: number
  /** Exactly these records (`memory_record_facets.record_ref`): a graph selection. */
  ref?: string[]
  /** A date range on the sort's time, as UTC ISO: `since` is inclusive, `until` exclusive. */
  since?: string; until?: string; date_field?: 'updated' | 'created' }
export type MemoryRecordQuery = { q: string; kind: MemoryRecordKind | 'all'; filters?: MemoryRecordFilters }
export type MemoryRecordSort = 'updated' | 'created' | 'confidence' | 'importance' | 'key' | 'match'
/** Per group, how many rows each value would list (`kind` counts ignore the kind). */
export type MemoryRecordFacets = Record<'kind' | MemoryRecordFacetGroup, Record<string, number>>
export type MemoryRecord = {
  kind: MemoryRecordKind; id: string; key?: string; value_json?: unknown; text: string
  source?: string; updated_at?: string; created_at?: string; derived_from?: string
  /** A fact's or lesson's confidence, or an experience's importance (one stored column). */
  importance?: number | null
  /** An experience's tags, as stored (a JSON list). */
  tags?: string
  /** The word match score, on a `sort=match` page. */
  match?: number
  revision: string
  metadata?: { revision?: number; category?: string; subject?: string; predicate?: string; status?: string; source_ref?: string; email_addresses?: string[]; pending_conflicts?: number }
}
export type MemoryRecordRef = { kind: MemoryRecordKind; id: string }
export type MemoryRecordSelection = { items: (MemoryRecordRef & { revision: string })[] }
  | { query: MemoryRecordQuery; exclude: MemoryRecordRef[] }
export type MemoryEditOperation = { type: 'replace_text'; find: string; replacement: string; match_case?: boolean }
  | { type: 'set'; value?: unknown; text?: string } | { type: 'forget' } | { type: 'restore'; revision_id: number }
export type MemoryEditPreview = {
  preview_id: string; expires_at: string; matched_count: number; changed_count: number; unchanged_count: number
  entries: { before: MemoryRecord; after: MemoryRecord | null; operation?: 'resolve' | 'correct' | 'forget' | 'restore' }[]
  preview_offset: number; preview_limit: number; preview_has_more: boolean; warnings: string[]
}
export type MemoryRecordRevision = { id: number; revision: number; base_revision: number; status: string; operation: string; source: string; before_json: string | null; after_json: string | null; metadata_json: string; created_at: string }

/** A row of the store's write log (`memory_events`), as `memory_audit.list_events` returns it. */
export type MemoryAuditEvent = {
  id: number; event_type: string; memory_type: string; memory_key: string | null
  old_value: string | null; new_value: string | null; source: string | null; created_at: string
  kind: MemoryRecordKind
}
export type MemoryAuditGroup = 'event' | 'kind' | 'source'
export type MemoryAuditQuery = { q: string; order: 'asc' | 'desc'; filters: Partial<Record<MemoryAuditGroup, string[]>>; since?: string; until?: string }
export type MemoryAuditPage = { events: MemoryAuditEvent[]; total: number; has_more: boolean; facets: Record<MemoryAuditGroup, Record<string, number>> }
