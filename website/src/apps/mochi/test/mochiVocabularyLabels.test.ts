/**
 * Every id-shaped value the UI shows must have a label.
 *
 * The failure this guards is not a crash — it is a lowercase English id appearing
 * in a Bengali sentence. It bit this app in the most visible way possible: the
 * mood shown in the Mood card went through `moodLabel()` while the "Top moods"
 * chips two cards away rendered `{m.mood}` raw, so the same value appeared
 * translated and untranslated on one screen. Activity kinds and watch kinds had
 * the same shape.
 *
 * The activity-kind test reads the BACKEND for its expectation rather than
 * restating a list, because `activity_log.log_activity` takes `kind` as a free
 * string: the vocabulary is whatever the call sites pass, so a new kind is added
 * without any type error and would render raw.
 */
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  ACTIVITY_TYPE_KEY,
  MOOD_LABEL_KEY,
  WATCH_KIND_KEY,
  activityTypeLabel,
  watchKindLabel,
} from '../i18nKeys'

const BACKEND = path.resolve(
  __dirname,
  '../../../../../src/kiro_crew/apps/builtins/mochi',
)

/** Kinds the backend passes to `log_activity` / `_log_activity`. */
function backendActivityKinds(): Set<string> {
  const kinds = new Set<string>()
  for (const file of readdirSync(BACKEND)) {
    if (!file.endsWith('.py')) continue
    const src = readFileSync(path.join(BACKEND, file), 'utf8')
    for (const m of src.matchAll(/_?log_activity\(\s*(?:[^,()]+,\s*)?"([a-z_]+)"/g)) {
      kinds.add(m[1])
    }
  }
  return kinds
}

describe('activity kinds', () => {
  it('every kind the backend writes has a label', () => {
    const backend = backendActivityKinds()
    expect(backend.size).toBeGreaterThan(0) // the scan itself must still work
    const missing = [...backend].filter((k) => !(k in ACTIVITY_TYPE_KEY))
    expect(missing).toEqual([])
  })

  it('the map carries no kind the backend never writes', () => {
    // `plan` is synthesized in `formatActivity`, not logged by the backend.
    const frontendOnly = new Set(['plan'])
    const backend = backendActivityKinds()
    const stale = Object.keys(ACTIVITY_TYPE_KEY).filter(
      (k) => !backend.has(k) && !frontendOnly.has(k),
    )
    expect(stale).toEqual([])
  })

  it('an unknown kind falls back to the raw value rather than blanking', () => {
    expect(activityTypeLabel('a-kind-from-the-future')).toBe('a-kind-from-the-future')
  })
})

describe('watch kinds', () => {
  it('covers the seed kinds and both time-triggered kinds', () => {
    expect(Object.keys(WATCH_KIND_KEY).sort()).toEqual([
      'custom',
      'meeting',
      'reminder',
      'url',
    ])
  })

  it('a category the user typed themselves passes through verbatim', () => {
    // The add form lets a user name their own category; that word is theirs.
    expect(watchKindLabel('flight-prices')).toBe('flight-prices')
  })
})

describe('no vocabulary is rendered raw', () => {
  it('the moods the dashboard charts all have labels', () => {
    // Both the Mood card and the Top-moods chips draw from this vocabulary; the
    // bug was one of them bypassing it.
    for (const mood of ['busy', 'curious', 'happy', 'scared', 'sleepy']) {
      expect(MOOD_LABEL_KEY).toHaveProperty(mood)
    }
  })
})
