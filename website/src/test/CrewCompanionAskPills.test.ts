/**
 * Every catalog key reached through a VARIABLE must still exist.
 *
 * The ask-row pills are the case that shipped broken. Their keys live in a
 * file-scope table and reach `i18nT` as `c.key`, not as a literal at the call site —
 * so `check-i18n-keys.mjs`, which resolves the literal argument, could not verify
 * them. They had been ported root-relative from the desktop app
 * (`panel.ask.in1h`), existed under no namespace here, and i18next answers a missing
 * key WITH THE KEY, so the three buttons rendered "panel.ask.in1h",
 * "panel.ask.tomorrow" and "panel.ask.daily" to the user. Nothing threw and the
 * buttons still worked: only the labels were wrong, which is exactly the shape of
 * defect a gate has to catch because a human reading the diff will not.
 *
 * This asserts against the real merged English catalog, so adding a fourth pill
 * without its copy fails here rather than on someone's screen.
 */
import { describe, it, expect } from 'vitest'

import { CATALOGS } from '../i18n'
import { ASK_CHOICE_KEYS } from '../apps/crew-companion/ReminderInput'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

const en = flatten((CATALOGS.en as { translation: unknown }).translation)

describe('ask-row pill labels resolve', () => {
  it('every pill key is fully namespaced', () => {
    for (const key of ASK_CHOICE_KEYS) {
      expect(key.startsWith('apps.crewCompanion.')).toBe(true)
    }
  })

  it('every pill key exists in the English catalog', () => {
    const missing = ASK_CHOICE_KEYS.filter((k) => !(k in en))
    expect(missing).toEqual([])
  })

  it('no pill renders its own key as the label', () => {
    // The exact user-visible symptom, asserted directly.
    for (const key of ASK_CHOICE_KEYS) {
      expect(en[key]).toBeTruthy()
      expect(en[key]).not.toBe(key)
      expect(en[key]).not.toContain('panel.ask')
    }
  })
})
