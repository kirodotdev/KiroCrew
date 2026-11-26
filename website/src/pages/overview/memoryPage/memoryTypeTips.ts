/**
 * The one-sentence (?) tip after each memory type on the memory page: every
 * Audit event, the three kinds and the three write origins. A tip that speaks of
 * the bot names it on a bot's own page (`bot`) and says "the bot" elsewhere. A
 * value with no tip (an unknown event code, a raw source tag) gets none.
 */
import { i18nT } from '../../../i18n/t'
import { REFUSED } from './memoryAuditLabels'

/** A tip's catalog key, and the key that names the bot when there is one. */
type Tip = [string, string?]

const EVENT_TIPS: Record<string, Tip> = {
  create: ['memoryAudit.tip_create'], update: ['memoryAudit.tip_update'], delete: ['memoryAudit.tip_delete'],
  restore: ['memoryAudit.tip_restore'], merge: ['memoryAudit.tip_merge'], conflict_skip: ['memoryAudit.tip_conflict_skip'],
  conflict_retire: ['memoryAudit.tip_conflict_retire'], [REFUSED]: ['memoryAudit.tip_refused'],
  injection_blocked: ['memoryAudit.tip_injection_blocked', 'memoryAudit.tip_injection_blocked_named'],
  migration: ['memoryAudit.tip_migration'],
}
const KIND_TIPS: Record<string, Tip> = {
  fact: ['memoryPage.tip_kind_fact'],
  directive: ['memoryPage.tip_kind_directive', 'memoryPage.tip_kind_directive_named'],
  episode: ['memoryPage.tip_kind_episode', 'memoryPage.tip_kind_episode_named'],
}
// `memory_record_facets.source_group` values with a fixed meaning.
const SOURCE_TIPS: Record<string, Tip> = {
  consolidation: ['memoryPage.tip_source_consolidation', 'memoryPage.tip_source_consolidation_named'],
  user_explicit: ['memoryPage.tip_source_user_explicit', 'memoryPage.tip_source_user_explicit_named'],
  taskrunner: ['memoryPage.tip_source_taskrunner'],
}

function tip(entry: Tip | undefined, bot?: string): string | undefined {
  if (!entry) return undefined
  const [plain, named] = entry
  return bot && named ? i18nT(named, { bot }) : i18nT(plain)
}

export const eventTip = (code: string, bot?: string) => tip(EVENT_TIPS[code], bot)
export const kindTip = (kind: string, bot?: string) => tip(KIND_TIPS[kind], bot)
export const sourceTip = (source: string, bot?: string) => tip(SOURCE_TIPS[source], bot)
