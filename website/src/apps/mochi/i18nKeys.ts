/**
 * Full literal translation keys for Mochi's runtime discriminants.
 *
 * These four vocabularies — animation state, mood, watch status, watch priority —
 * arrive as VALUES at runtime, so the natural way to translate them is to
 * interpolate the value into the key. That is exactly what this module exists to
 * avoid, and `src/i18n/dynamicKeys.test.ts` enforces it: a key assembled by
 * interpolation appears nowhere in the source, so the extractor cannot find it,
 * the unused-key sweep counts it as dead and may prune it, and the `en-XA`
 * pseudolocale cannot flag it as untranslated. It then renders as the raw key the
 * first time a value is added without a matching entry.
 *
 * Mapping value -> full literal key keeps every key greppable, and the lookups
 * below fall back to the raw value on purpose rather than throwing: a pack may
 * declare a slot this build has no label for, and showing the slot name beats an
 * empty label or a crash.
 *
 * Key sets mirror `apps.mochi.{state,mood,watchPanel.status,watchPanel.priority}`
 * in the catalog. Note the leaves are NOT snake_cased the way the rest of the
 * catalog is: each one has to equal the value the code actually holds
 * (`peekThinking` is an animation slot name, `approval_pending` a state id), so
 * these leaves are data, not free-form naming.
 */
import { i18nT } from '../../i18n/t'

export const STATE_LABEL_KEY = {
  approval_pending: 'apps.mochi.state.approval_pending',
  done: 'apps.mochi.state.done',
  error: 'apps.mochi.state.error',
  hiding: 'apps.mochi.state.hiding',
  idle: 'apps.mochi.state.idle',
  listening: 'apps.mochi.state.listening',
  offline: 'apps.mochi.state.offline',
  peekThinking: 'apps.mochi.state.peekThinking',
  peeking: 'apps.mochi.state.peeking',
  thinking: 'apps.mochi.state.thinking',
  walking: 'apps.mochi.state.walking',
  working: 'apps.mochi.state.working',
} as const

export const MOOD_LABEL_KEY = {
  busy: 'apps.mochi.mood.busy',
  curious: 'apps.mochi.mood.curious',
  happy: 'apps.mochi.mood.happy',
  scared: 'apps.mochi.mood.scared',
  sleepy: 'apps.mochi.mood.sleepy',
} as const

export const WATCH_STATUS_KEY = {
  cancelled: 'apps.mochi.watchPanel.status.cancelled',
  done: 'apps.mochi.watchPanel.status.done',
  expired: 'apps.mochi.watchPanel.status.expired',
  failed: 'apps.mochi.watchPanel.status.failed',
  triggered: 'apps.mochi.watchPanel.status.triggered',
  watching: 'apps.mochi.watchPanel.status.watching',
} as const

export const WATCH_PRIORITY_KEY = {
  high: 'apps.mochi.watchPanel.priority.high',
  low: 'apps.mochi.watchPanel.priority.low',
  normal: 'apps.mochi.watchPanel.priority.normal',
} as const

/**
 * Activity-log entry kinds.
 *
 * The five backend kinds are the literals `log_activity(..., kind, ...)` is called
 * with (`activity_log.py` takes `kind` as a free string, so the vocabulary lives at
 * the call sites); `plan` is synthesized in the frontend by `formatActivity`, which
 * puts the current narrative at the top of the list. A drift guard in
 * `test_mochi_activity.py` ties this map to those call sites.
 */
export const ACTIVITY_TYPE_KEY = {
  budget: 'apps.mochi.activity.type.budget',
  memory: 'apps.mochi.activity.type.memory',
  presence: 'apps.mochi.activity.type.presence',
  notification: 'apps.mochi.activity.type.notification',
  plan: 'apps.mochi.activity.type.plan',
  sleep: 'apps.mochi.activity.type.sleep',
  spawn: 'apps.mochi.activity.type.spawn',
  system: 'apps.mochi.activity.type.system',
} as const

/**
 * Watch-item kinds.
 *
 * `url` and `custom` are the two seed kinds a user picks in the add form;
 * `reminder` and `meeting` are time-triggered and created by the agent, and the
 * panel branches on them (they have no check target), so they surface as labels
 * too.
 */
export const WATCH_KIND_KEY = {
  custom: 'apps.mochi.watchPanel.kind.custom',
  meeting: 'apps.mochi.watchPanel.kind.meeting',
  reminder: 'apps.mochi.watchPanel.kind.reminder',
  url: 'apps.mochi.watchPanel.kind.url',
} as const


/*
 * Each lookup below indexes its map INSIDE the `i18nT(...)` call rather than through a
 * local `const key = MAP[...]` first. Both forms behave identically at runtime, but
 * `check-i18n-keys.mjs` resolves file-scope bindings only, so a function-local
 * intermediate makes the call site unresolvable — and an unresolvable key is one the
 * gate cannot verify exists, which exempts the whole file from the very check this
 * module was written to enable. Indexing in place resolves to the union of the map's
 * values, so every key here is checked against the catalog. Keep it that way.
 */

/** Localized animation-state label, falling back to the raw slot name. */
export function stateLabel(state: string): string {
  const k = state as keyof typeof STATE_LABEL_KEY
  return STATE_LABEL_KEY[k] ? i18nT(STATE_LABEL_KEY[k]) : state
}

/** Localized mood label, falling back to the raw mood id. */
export function moodLabel(mood: string): string {
  const k = mood as keyof typeof MOOD_LABEL_KEY
  return MOOD_LABEL_KEY[k] ? i18nT(MOOD_LABEL_KEY[k]) : mood
}

/**
 * Label for a slot that may be either a state or a mood.
 *
 * Pack editors list both vocabularies in one grid, which is why the two-step
 * lookup lives here rather than at the call site: the previous form
 * (`state(k) || mood(k) || k`) worked only because a miss returned the key, so
 * adding a real label whose text happened to be falsy would have broken it.
 */
export function slotLabel(slot: string): string {
  const sk = slot as keyof typeof STATE_LABEL_KEY
  if (STATE_LABEL_KEY[sk]) return i18nT(STATE_LABEL_KEY[sk])
  const mk = slot as keyof typeof MOOD_LABEL_KEY
  return MOOD_LABEL_KEY[mk] ? i18nT(MOOD_LABEL_KEY[mk]) : slot
}

/** Localized watch-item status, falling back to the raw status. */
export function watchStatusLabel(status: string): string {
  const k = status as keyof typeof WATCH_STATUS_KEY
  return WATCH_STATUS_KEY[k] ? i18nT(WATCH_STATUS_KEY[k]) : status
}

/** Localized watch-item priority, falling back to the raw priority. */
export function watchPriorityLabel(priority: string): string {
  const k = priority as keyof typeof WATCH_PRIORITY_KEY
  return WATCH_PRIORITY_KEY[k] ? i18nT(WATCH_PRIORITY_KEY[k]) : priority
}

/** Localized activity-log entry kind, falling back to the raw kind. */
export function activityTypeLabel(type: string): string {
  const k = type as keyof typeof ACTIVITY_TYPE_KEY
  return ACTIVITY_TYPE_KEY[k] ? i18nT(ACTIVITY_TYPE_KEY[k]) : type
}

/** Localized watch-item kind, falling back to the raw kind. */
export function watchKindLabel(kind: string): string {
  const k = kind as keyof typeof WATCH_KIND_KEY
  return WATCH_KIND_KEY[k] ? i18nT(WATCH_KIND_KEY[k]) : kind
}
