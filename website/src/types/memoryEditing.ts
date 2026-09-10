export type MemoryRecordKind = 'fact' | 'directive' | 'episode'
export type MemoryRecordQuery = { q: string; kind: MemoryRecordKind | 'all' }
export type MemoryRecord = {
  kind: MemoryRecordKind; id: string; key?: string; value_json?: unknown; text: string
  source?: string; updated_at?: string; created_at?: string; derived_from?: string
  revision: string
  metadata?: { revision?: number; category?: string; subject?: string; predicate?: string; status?: string; source_ref?: string; email_addresses?: string[]; pending_conflicts?: number }
}
export type MemoryRecordRef = { kind: MemoryRecordKind; id: string }
export type MemoryRecordSelection = { items: (MemoryRecordRef & { revision: string })[] }
  | { query: MemoryRecordQuery; exclude: MemoryRecordRef[] }
export type MemoryEditOperation = { type: 'replace_text'; find: string; replacement: string; match_case?: boolean }
  | { type: 'set'; value?: unknown; text?: string } | { type: 'forget' }
export type MemoryEditPreview = {
  preview_id: string; expires_at: string; matched_count: number; changed_count: number; unchanged_count: number
  entries: { before: MemoryRecord; after: MemoryRecord | null; operation?: 'resolve' | 'correct' | 'forget' }[]
  preview_offset: number; preview_limit: number; preview_has_more: boolean; warnings: string[]
}
export type MemoryRecordRevision = { id: number; revision: number; base_revision: number; status: string; operation: string; source: string; before_json: string | null; after_json: string | null; metadata_json: string; created_at: string }
