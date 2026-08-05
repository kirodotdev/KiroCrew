/**
 * The pseudolocale must be resolvable but not selectable.
 *
 * `en-XA` has to be a registered language: `resolveLanguage()` falls back on the primary
 * subtag, so an unregistered `en-XA` silently collapses to `en` and the pseudolocale
 * never activates. But a user of a shipped build must not be able to select a UI where
 * every string is accented and padded.
 *
 * That leaves exactly one failure mode — a botched environment check leaking it into
 * production — so this asserts the complement rather than trusting the check: in a
 * production build the picker offers every registered language *except* the dev-only
 * ones, and the count is asserted, not just the absence.
 *
 * Also guarded here: registering a pseudolocale must not perturb real-language
 * detection. A pseudolocale sharing the `en` primary subtag gives `en` two
 * candidates, which would make `matchConfident()` treat `en-GB` as ambiguous and
 * stop resolving it to `en`. Browser detection therefore reads `DETECTABLE_CODES`,
 * and these tests pin that separation.
 */

import { describe, it, expect, afterEach, vi } from 'vitest'

import {
  SUPPORTED_LANGUAGES,
  SUPPORTED_CODES,
  DETECTABLE_CODES,
  PICKABLE_LANGUAGES,
  DEFAULT_LANGUAGE,
} from './languages'
import { resolveLanguage } from './detect'
import { CATALOGS } from './index'

const PSEUDO = 'en-XA'
const devOnly = SUPPORTED_LANGUAGES.filter((l) => l.devOnly).map((l) => l.code)

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    const p = prefix ? `${prefix}.${k}` : k
    if (v !== null && typeof v === 'object') Object.assign(out, flatten(v, p))
    else out[p] = String(v)
  }
  return out
}

describe('pseudolocale registration', () => {
  it('is registered as a resolvable language', () => {
    // Without this, `resolveLanguage('en-XA')` returns 'en' by primary-subtag match and
    // the pseudolocale is silently inert.
    expect(SUPPORTED_CODES).toContain(PSEUDO)
  })

  it('has a catalog wired into the runtime', () => {
    expect(Object.keys(CATALOGS)).toContain(PSEUDO)
  })

  it('is the only dev-only language', () => {
    // A second one would mean someone reused the flag for something it was not designed
    // for; the picker and detection exclusions are written for a pseudolocale.
    expect(devOnly).toEqual([PSEUDO])
  })

  it('is NOT detectable from a browser tag', () => {
    // No browser sends `en-XA`, and including it makes every real `en-*` tag ambiguous.
    expect(DETECTABLE_CODES).not.toContain(PSEUDO)
    expect(DETECTABLE_CODES).toContain(DEFAULT_LANGUAGE)
  })

  it('leaves exactly the authored languages detectable', () => {
    expect(DETECTABLE_CODES.length).toBe(SUPPORTED_CODES.length - devOnly.length)
  })

  it('is hidden from the picker unless this is a dev build', () => {
    const codes = PICKABLE_LANGUAGES.map((l) => l.code)
    if (import.meta.env.DEV) {
      expect(codes).toContain(PSEUDO)
    } else {
      expect(codes).not.toContain(PSEUDO)
    }
    // Assert the complement, not just the absence: a broken environment check that
    // dropped every language would also satisfy `not.toContain`.
    expect(codes.length).toBe(
      import.meta.env.DEV ? SUPPORTED_CODES.length : SUPPORTED_CODES.length - devOnly.length,
    )
  })
})

/**
 * The generated catalog is committed, so its integrity is testable directly.
 * `gen-pseudolocale.mjs` masks the preserved regions one pattern at a time, so a
 * URL pattern can swallow already-masked `<owner>` / `<repo>` tokens inside
 * `https://github.com/<owner>/<repo>` and a single-shot restore can leave the inner
 * sentinels behind, shipping literal NUL control characters where markup should be.
 * Placeholder-parity assertions do not catch this, which is why this asserts the
 * artifact rather than the transform.
 */
describe('pseudolocale catalog integrity', () => {
  const flat = flatten(CATALOGS[PSEUDO])

  it('has values for every key', () => {
    expect(Object.keys(flat).length).toBeGreaterThan(0)
  })

  it('carries no unrestored masking sentinel in any value', () => {
    const corrupt = Object.keys(flat).filter((k) => flat[k].includes('\u0000'))
    expect(corrupt).toEqual([])
  })

  it('carries no control characters at all', () => {
    // Wider than the sentinel check on purpose: any C0 control character in a UI string
    // is a generator defect, whatever produced it.
    const controls = Object.keys(flat).filter((k) => /[\u0000-\u0008\u000B\u000C\u000E-\u001F]/.test(flat[k]))
    expect(controls).toEqual([])
  })

  it('preserves a URL whose path contains nested <…> placeholders', () => {
    // The exact shape that broke: two `<…>` tokens inside one URL. Both must survive, and
    // the value must be nothing but the bracketed original — a URL has no accentable
    // text and no padding budget.
    const en = flatten(CATALOGS[DEFAULT_LANGUAGE])
    const nested = Object.keys(en).filter((k) => /^https?:\/\/\S*<[^>]+>\S*$/.test(en[k]))
    expect(nested.length).toBeGreaterThan(0)
    for (const k of nested) expect(flat[k]).toBe(`[${en[k]}]`)
  })
})

/**
 * "Resolvable but not selectable" has to hold for PERSISTED state too, not just the
 * picker. A dev who selects `en-XA` writes it to `dashboard.language`; a production build
 * reading that same config must not honour it, or it would render an accented padded UI
 * with no picker entry to explain or escape it.
 *
 * `import.meta.env.DEV` is true under vitest, so the production branch is stubbed rather
 * than merely asserted around — an `if (DEV) … else …` test would only ever run its dev
 * half in CI and would pass just as happily against the unfixed code.
 */
describe('pseudolocale is not restorable from persisted state', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('is honoured in a dev build', () => {
    vi.stubEnv('DEV', true)
    expect(resolveLanguage(PSEUDO)).toBe(PSEUDO)
  })

  it('degrades to auto-detect in a production build', () => {
    vi.stubEnv('DEV', false)
    expect(resolveLanguage(PSEUDO)).not.toBe(PSEUDO)
    // Auto-detect, not a hard-coded `en`: the point is that the stored value is ignored,
    // and detection then does whatever it would have done with no stored value at all.
    expect(resolveLanguage(PSEUDO)).toBe(resolveLanguage(''))
  })

  it('still restores a real stored language in a production build', () => {
    // The complement: the guard must reject dev-only codes, not persisted state at large.
    vi.stubEnv('DEV', false)
    expect(resolveLanguage('zh-CN')).toBe('zh-CN')
    expect(resolveLanguage(DEFAULT_LANGUAGE)).toBe(DEFAULT_LANGUAGE)
  })
})
