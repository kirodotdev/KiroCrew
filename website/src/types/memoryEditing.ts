export type MemoryRecordKind = 'fact' | 'directive' | 'episode'
/** The facet groups `GET /api/memory/records` filters on (`memory_record_facets.FACET_GROUPS`). */
export type MemoryRecordFacetGroup = 'category' | 'applies' | 'scope' | 'source' | 'surface' | 'crew' | 'tag' | 'strength'
/** Facet filters plus the confidence range; part of the query, so "select all
 *  matching" reaches exactly the filtered rows. */
export type MemoryRecordFilters = Partial<Record<MemoryRecordFacetGroup, string[]>> & { conf_min?: number; conf_max?: number
  /** Exactly these records (`memory_record_facets.record_ref`): a graph selection. */
  ref?: string[]
  /** A date range on the sort's time, as UTC ISO: `since` is inclusive, `until` exclusive. */
  since?: string; until?: string; date_field?: 'updated' | 'created' }
export type MemoryRecordQuery = { q: string; kind: MemoryRecordKind | 'all'; filters?: MemoryRecordFilters }
export type MemoryRecordSort = 'updated' | 'created' | 'confidence' | 'importance' | 'strength' | 'key' | 'match'
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
  /** A V1 experience's strength in recall today (`memory_decay.record_decay`). */
  decay?: MemoryRecordDecay
  revision: string
  metadata?: { revision?: number; category?: string; subject?: string; predicate?: string; status?: string; source_ref?: string; email_addresses?: string[]; pending_conflicts?: number }
}
export type MemoryStrengthState = 'strong' | 'fading' | 'faded'
/** Recall's score for the row at an exact match, and what sets it. */
export type MemoryRecordDecay = {
  strength: number
  /** The strength on the day it was written: 0.7 + 0.3 × importance. */
  start: number
  /** Per day; from `memory.decay_rates` for `rate_tag`, else the default. */
  rate: number; rate_tag: string | null
  days_old: number
  /** Days until it is under the fade line: 0 once it is, null when it never will be. */
  days_to_line: number | null
  state: MemoryStrengthState
}
/** The store's fade rates and line (`memory_decay.store_decay`); V1 stores only. */
export type MemoryStoreDecay = {
  fade_line: number
  /** The importance the per-rate lines are about, and its strength on the day it is written. */
  importance: number; start: number
  /** Each rate, and the age at which that row is first under the line (null: never). */
  rates: { tag: string | null; rate: number; days_to_line: number | null }[]
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
