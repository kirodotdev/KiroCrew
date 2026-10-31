/**
 * Text the dashboard did not write is displayed exactly as stored.
 *
 * Tool-step titles, question-card options, reminders and command-bar queries
 * reach `i18nT()` as interpolation values. These tests pin that such a value is
 * shown verbatim and is never handed to i18next at all, so i18next cannot
 * resolve a nesting reference in it, fill a placeholder in it, or scan it.
 *
 * The nesting marker is built from char codes so this file never contains it
 * literally.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { i18next } from './index'
import { i18nT } from './t'
import { renderToolAction } from '../utils/toolCallTitle'

const NEST = String.fromCharCode(36, 116, 40)

let n = 0
let KEY = ''
beforeEach(() => {
  n += 1
  KEY = `test.literalValues.k${n}`
  i18next.addResource('en', 'translation', KEY, 'Ran {{command}}')
  i18next.addResource('en', 'translation', 'test.literalValues.secret', 'RESOLVED')
})
afterEach(() => { vi.restoreAllMocks() })

/** Every string i18next.t receives during `fn`, in its options or as its key. */
function seenByI18next(fn: () => void): string[] {
  const seen: string[] = []
  const real = i18next.t.bind(i18next)
  vi.spyOn(i18next, 't').mockImplementation(((k: unknown, opts?: Record<string, unknown>) => {
    seen.push(String(k))
    for (const v of Object.values(opts ?? {})) if (typeof v === 'string') seen.push(v)
    return real(k as string, opts as never)
  }) as never)
  fn()
  return seen
}

describe('interpolation values are displayed literally', () => {
  const cases: Array<[string, string]> = [
    ['a well-formed nesting reference', `grep ${NEST}test.literalValues.secret)`],
    ['a placeholder', 'echo {{productName}} {{command}}'],
    ['an unclosed nesting marker', `grep '${NEST}abc"`],
  ]

  for (const [name, stored] of cases) {
    it(`shows ${name} verbatim and never hands it to i18next`, () => {
      let out = ''
      const seen = seenByI18next(() => { out = i18nT(KEY, { command: stored }) })
      expect(out).toBe(`Ran ${stored}`)
      expect(seen.some(s => s.includes(stored))).toBe(false)
    })
  }

  it('returns at once for a long unclosed nesting marker', () => {
    // On an i18next whose nesting scan backtracks, this value alone takes
    // seconds; text that never reaches i18next takes no time at all.
    const stored = `${NEST}${'x'.repeat(26)}"`
    const t0 = performance.now()
    const out = i18nT(KEY, { command: stored })
    expect(performance.now() - t0).toBeLessThan(150)
    expect(out).toBe(`Ran ${stored}`)
  })

  it('keeps two values apart and puts back a value repeated in the template', () => {
    i18next.addResource('en', 'translation', KEY, '{{a}} / {{b}} / {{a}}')
    expect(i18nT(KEY, { a: `${NEST}x)`, b: '{{a}}' })).toBe(`${NEST}x) / {{a}} / ${NEST}x)`)
  })

  it('still interpolates numbers and still selects plurals by count', () => {
    i18next.addResource('en', 'translation', `${KEY}_one`, '{{count}} step for {{who}}')
    i18next.addResource('en', 'translation', `${KEY}_other`, '{{count}} steps for {{who}}')
    expect(i18nT(KEY, { count: 1, who: '{{count}}' })).toBe('1 step for {{count}}')
    expect(i18nT(KEY, { count: 3, who: 'me' })).toBe('3 steps for me')
  })

  it('shows a tool-step title built from a command verbatim', () => {
    const query = `${NEST}test.literalValues.secret) {{productName}}`
    let title = ''
    const seen = seenByI18next(() => { title = renderToolAction({ type: 'search', query }) })
    expect(title).toContain(query)
    expect(title).not.toContain('RESOLVED')
    expect(seen.some(s => s.includes(query))).toBe(false)
  })
})
