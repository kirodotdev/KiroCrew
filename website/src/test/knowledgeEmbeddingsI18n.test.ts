/**
 * The Knowledge footer's embedded-item count must come from the catalog.
 *
 * It was built as a template literal — `embeddings (${n})` — directly beside a
 * sibling branch that already used `i18nT`, so every non-English user saw an
 * English word in an otherwise translated row.
 *
 * This is pinned by a test rather than by the i18n gate on purpose: that gate is
 * diff-scoped ([added-lines] checks only lines the change wrote), so a
 * pre-existing literal on an untouched line is invisible to it. Verified —
 * restoring the literal keeps the gate green. A source assertion is the only
 * thing that catches a regression here.
 */
import { describe, it, expect } from 'vitest'

const KEY_PATH = ['pages', 'knowledge', 'index', 'embeddings_count'] as const

// Every shipped locale, plus the manual English source and the pseudolocale.
const LOCALES = [
  'en.manual', 'en-XA', 'zh-CN', 'ja', 'ko', 'de',
  'fr', 'es', 'pt', 'ru', 'it', 'hi', 'bn',
]

function dig(catalog: unknown): unknown {
  return KEY_PATH.reduce<unknown>(
    (node, part) => (node as Record<string, unknown> | undefined)?.[part],
    catalog,
  )
}

describe('Knowledge embedded-count string', () => {
  it('is rendered through the catalog, not a template literal', async () => {
    const src = (await import('../pages/knowledge/index.tsx?raw')).default as string
    expect(src).toContain("i18nT('pages.knowledge.index.embeddings_count'")
    // The exact shape that shipped the bug.
    expect(src).not.toMatch(/`embeddings \(\$\{/)
    // Nor any bare English "embeddings (" outside a catalog call.
    expect(src).not.toMatch(/['"`]embeddings \(/)
  })

  it('is defined in every shipped locale', async () => {
    const missing: string[] = []
    for (const tag of LOCALES) {
      const catalog = (await import(`../i18n/locales/${tag}.json`)).default
      const value = dig(catalog)
      if (typeof value !== 'string' || value.length === 0) missing.push(tag)
    }
    expect(missing, `locales missing ${KEY_PATH.join('.')}`).toEqual([])
  })

  it('keeps the count interpolated in every locale', async () => {
    const withoutValue: string[] = []
    for (const tag of LOCALES) {
      const catalog = (await import(`../i18n/locales/${tag}.json`)).default
      const value = dig(catalog)
      // A translation that drops {{value}} silently hides the number.
      if (typeof value === 'string' && !value.includes('{{value}}')) {
        withoutValue.push(tag)
      }
    }
    expect(withoutValue, 'locales that dropped the {{value}} placeholder').toEqual([])
  })
})
