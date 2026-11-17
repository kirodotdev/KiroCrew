/**
 * Devanagari (hi) and Bengali (bn) hang each word from one headline stroke and join
 * letters into conjuncts, so letter-spacing pulls a word apart (or crowds it, when
 * negative).
 * The dashboard's eyebrows and small labels carry tracking tuned for Latin capitals,
 * so one rule in `styles/content-fit.css` puts letter-spacing back to normal under
 * those two UI languages. What these tests pin is invisible from reading that rule:
 * that it reaches every way a core stylesheet adds tracking, and that it wins the
 * index.css rule it ties with on specificity.
 *
 * Built-in app pages under `src/apps/` are out of scope: those pages are leaving core
 * (website/AGENTS.md, "No built-in app pages").
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'
import { readIndexCss } from '../test/indexCssSource'

const SRC = join(__dirname, '..')

interface Rule { selector: string; body: string; at: number }

/** The innermost rules of a stylesheet, comments dropped; an @media body yields its rules. */
function rules(css: string): Rule[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, '')
  return [...text.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({ selector: m[1].trim(), body: m[2], at: m.index ?? 0 }))
}

/** The letter-spacing a rule sets, or null when it sets none or only the default. */
function tracking(rule: Rule): string | null {
  const value = rule.body.match(/(?<![\w-])letter-spacing\s*:\s*([^;]+)/)?.[1].trim()
  return value && !/^(normal|inherit|initial|unset|0[a-z]*)$/.test(value) ? value : null
}

const INDEX_RULES = rules(readIndexCss())
const RESET = INDEX_RULES.find(
  (r) => r.selector.includes(':lang(hi)') && r.selector.includes(':lang(bn)') && /letter-spacing\s*:\s*normal/.test(r.body),
)
const RESET_CLASSES = [...(RESET?.selector ?? '').matchAll(/\.([\w-]+)/g)].map((m) => m[1])

describe('joining scripts take no letter-spacing (#34951)', () => {
  it('puts the tracking-* utilities back to normal under an html:lang(hi) or html:lang(bn) UI', () => {
    expect(RESET, 'no hi/bn letter-spacing reset in index.css').toBeDefined()
    expect(RESET!.selector).toMatch(/^html:is\(:lang\(hi\),\s*:lang\(bn\)\)\s/)
    expect(RESET!.selector).toContain('[class*="tracking-"]')
  })

  it('names every class a core stylesheet tracks itself, and comes after each such index.css rule', () => {
    const sheets = (readdirSync(SRC, { recursive: true }) as string[])
      .filter((f) => f.endsWith('.css') && f.split(/[\\/]/)[0] !== 'apps')
    expect(sheets).toContain(join('styles', 'content-fit.css'))
    for (const sheet of sheets) {
      for (const rule of rules(readFileSync(join(SRC, sheet), 'utf8'))) {
        if (tracking(rule) === null) continue
        // Each selector of a list, split at commas outside :is() / :not().
        for (const selector of rule.selector.split(/,(?![^(]*\))/)) {
          const cls = selector.match(/\.([\w-]+)[^.]*$/)?.[1]
          expect(cls, `${sheet}: "${selector.trim()}" tracks text without a class to name`).toBeDefined()
          expect(RESET_CLASSES, `${sheet}: .${cls} sets letter-spacing but the hi/bn reset misses it`).toContain(cls)
        }
      }
    }
    for (const rule of INDEX_RULES) {
      if (tracking(rule) !== null) expect(rule.at, `"${rule.selector}" comes after the hi/bn reset`).toBeLessThan(RESET!.at)
    }
  })
})
