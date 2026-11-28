/**
 * How the Audit tab names each `memory_events.event_type`, which tone its chip
 * takes, and the one-line rule behind each refusal. The codes are the ones
 * `vector_memory_runtime` writes (`SemanticRejectCode`, `_log_event`) plus the
 * memory page's own edits (`memory_edit._operation_label`); an unknown code
 * shows as stored.
 */
import { i18nT } from '../../../i18n/t'

/** Every refusal code (`SemanticRejectCode`'s audited values but the unsafe-text block). */
export const REFUSAL_CODES = ['allowlist_reject', 'low_confidence', 'reserved_prefix', 'value_empty', 'work_state', 'consolidation_value_size'] as const
/** The rail's Refused item stands for every refusal code. */
export const REFUSED = 'refused'

const LABEL_KEYS: Record<string, string> = {
  create: 'memoryAudit.event_create', update: 'memoryAudit.event_update', delete: 'memoryAudit.event_delete',
  restore: 'memoryAudit.event_restore', correct: 'memoryAudit.event_correct', resolve: 'memoryAudit.event_resolve', merge: 'memoryAudit.event_merge', conflict_skip: 'memoryAudit.event_conflict_skip',
  conflict_retire: 'memoryAudit.event_conflict_retire', migration: 'memoryAudit.event_migration',
  injection_blocked: 'memoryAudit.event_injection_blocked',
  allowlist_reject: 'memoryAudit.event_allowlist_reject', low_confidence: 'memoryAudit.event_low_confidence',
  reserved_prefix: 'memoryAudit.event_reserved_prefix', value_empty: 'memoryAudit.event_value_empty',
  work_state: 'memoryAudit.event_work_state', consolidation_value_size: 'memoryAudit.event_consolidation_value_size',
  [REFUSED]: 'memoryAudit.event_refused',
}
// The rule each refusal applies (`vector_memory_runtime/semantic.validate_semantic`,
// `memory_kind_routing`, `_MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS`).
const WHY_KEYS: Record<string, string> = {
  allowlist_reject: 'memoryAudit.why_allowlist_reject', low_confidence: 'memoryAudit.why_low_confidence',
  reserved_prefix: 'memoryAudit.why_reserved_prefix', value_empty: 'memoryAudit.why_value_empty',
  work_state: 'memoryAudit.why_work_state', consolidation_value_size: 'memoryAudit.why_consolidation_value_size',
  injection_blocked: 'memoryAudit.why_injection_blocked',
}

export const isRefusal = (code: string) => (REFUSAL_CODES as readonly string[]).includes(code)
/** A refusal, a block or a skip: the event's value was never stored. */
export const notSaved = (code: string) => isRefusal(code) || code === 'injection_blocked' || code === 'conflict_skip'
export const eventLabel = (code: string) => LABEL_KEYS[code] ? i18nT(LABEL_KEYS[code]) : code
export const eventWhy = (code: string) => WHY_KEYS[code] ? i18nT(WHY_KEYS[code]) : ''

/** Red for a refusal or a block, amber for a skip, green for every write that landed.
 *  The text is mixed toward the body text so it holds 4.5:1 on its tinted fill in
 *  dark palettes, whose --ok / --danger alone fall to about 3:1 there. */
export function eventTone(code: string): string {
  if (isRefusal(code) || code === 'injection_blocked' || code === REFUSED) return 'bg-danger-subtle text-[var(--danger-body)]'
  if (code === 'conflict_skip') return 'bg-warn-subtle text-warn-text'
  return 'bg-ok-subtle text-[color-mix(in_srgb,var(--ok-text)_60%,var(--text))]'
}

/** The rail's Event values: one Refused item in place of the refusal codes, with their sum. */
export function railEvents(counts: Record<string, number>): Record<string, number> {
  const out: Record<string, number> = {}
  for (const [code, count] of Object.entries(counts)) {
    const key = isRefusal(code) ? REFUSED : code
    out[key] = (out[key] || 0) + count
  }
  return out
}

/** The event codes a rail pick sends: Refused stands for every refusal code. */
export const eventCodes = (picked: string[]) => picked.flatMap(value => value === REFUSED ? [...REFUSAL_CODES] : [value])
