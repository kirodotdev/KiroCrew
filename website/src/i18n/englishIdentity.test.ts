/**
 * English-rendering identity guard.
 *
 * The point of this file: a ~4200-site mechanical conversion is only reviewable
 * if "did English rendering change?" is answered by a machine rather than by
 * eyeballing 258 diffs. These checks assert the properties that would break if
 * the codemod mangled a string — independently of the ~4000 existing DOM
 * assertions that already match visible English text (those are the other half
 * of the safety net; this file catches classes of damage a screen-render test
 * can miss, like a stray key or a lost placeholder).
 */

import { describe, it, expect } from 'vitest'

import en from './locales/en.json'
import enManual from './locales/en.manual.json'

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

const FLAT = flatten(en)
const ENTRIES = Object.entries(FLAT)

describe('en catalog integrity', () => {
  it('holds the full extracted corpus', () => {
    // A floor, not an exact count — the point is to catch a catalog that
    // collapsed (a broken codemod run writing near-nothing), which would make
    // every t() fall through to its raw key.
    expect(ENTRIES.length).toBeGreaterThan(3000)
  })

  it('has no unresolved HTML entity in any value', () => {
    // JSX decodes entities before render; a JSON catalog does not. An entity
    // left encoded here renders literally as `&rarr;` / `&amp;` in the UI.
    // This actually happened on the first codemod run (90 keys affected), which
    // is why it is asserted rather than trusted.
    const offenders = ENTRIES
      .filter(([, v]) => /&(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#[xX][0-9a-fA-F]+);/.test(v))
      .map(([k, v]) => `${k} = ${JSON.stringify(v)}`)
    expect(offenders, offenders.slice(0, 5).join('\n')).toEqual([])
  })

  it('has no value that is only whitespace', () => {
    const blank = ENTRIES.filter(([, v]) => v.trim() === '').map(([k]) => k)
    expect(blank, blank.slice(0, 5).join(', ')).toEqual([])
  })

  it('has no value without a letter (pure punctuation is not translatable)', () => {
    // A letterless value is never prose — it is a glyph the codemod should have
    // skipped, and it reaches translators as an untranslatable key with a
    // meaningless auto-name like `text_2`. The dangerous case was
    // `pages.schedulePage.text = '"?'` — the closing quote AND question mark of
    // a delete-confirmation heading, which a translator dropping it would have
    // silently turned into a statement. Such glyphs belong inline in the JSX.
    const offenders = ENTRIES
      .filter(([, v]) => !/[A-Za-z]/.test(v))
      .map(([k, v]) => `${k} = ${JSON.stringify(v)}`)
    expect(offenders, offenders.slice(0, 8).join('\n')).toEqual([])
  })

  it('has no value that looks like a translation key', () => {
    // A dotted, space-free, all-lowercase value is the signature of a key
    // accidentally captured as its own text.
    const keyish = ENTRIES
      .filter(([, v]) => /^[a-z][a-zA-Z0-9]*(?:\.[a-z][a-zA-Z0-9_]*){2,}$/.test(v.trim()))
      .map(([k, v]) => `${k} = ${v}`)
    expect(keyish, keyish.slice(0, 5).join('\n')).toEqual([])
  })

  it('has no value containing raw JSX or TS syntax', () => {
    // Signals the extractor swallowed an expression rather than a text node.
    // Well-formed `{{placeholders}}` are stripped first: they are legitimate
    // i18next interpolation (the next test validates their shape), and the
    // single-brace JSX pattern below otherwise matches their inner braces.
    const codeish = ENTRIES
      .filter(([, v]) => /\{[a-zA-Z_$][\w$]*\}|=>|<\/[a-zA-Z]/.test(v.replace(/\{\{[^}]*\}\}/g, '')))
      .map(([k, v]) => `${k} = ${JSON.stringify(v)}`)
    expect(codeish, codeish.slice(0, 5).join('\n')).toEqual([])
  })

  it('uses only well-formed {{placeholders}}', () => {
    // `{{ }}`, `{{1}}`, or a single-brace `{count}` would silently interpolate
    // to nothing at runtime.
    const bad: string[] = []
    for (const [k, v] of ENTRIES) {
      for (const ph of v.match(/\{\{[^}]*\}\}/g) ?? []) {
        if (!/^\{\{\s*[a-zA-Z_][\w.]*\s*\}\}$/.test(ph)) bad.push(`${k}: ${ph}`)
      }
    }
    expect(bad, bad.slice(0, 5).join(' | ')).toEqual([])
  })

  it('does not duplicate a key between the generated and manual catalogs', () => {
    // `en.json` is regenerated wholesale by the codemod; a key present in both
    // files would be silently overwritten on the next run, so the manual
    // catalog must stay disjoint.
    const manual = Object.keys(flatten(enManual))
    const collisions = manual.filter(k => k in FLAT)
    expect(collisions, collisions.join(', ')).toEqual([])
  })
})

describe('no user-facing English left outside the catalog', () => {
  /**
   * `window.confirm`/`alert` strings are CALL ARGUMENTS, not JSX, so the codemod
   * never saw them — 28 of them (including destructive-action confirmations like
   * "Delete this campaign… This cannot be undone.") stayed hardcoded English and
   * would have shipped that way in every language. This guard is the only thing
   * that makes that class visible, since nothing else fails: it type-checks, and
   * the tests assert the English text.
   */
  it('has no hardcoded string literal in a confirm()/alert() call', async () => {
    const { readdirSync, readFileSync } = await import('node:fs')
    const { join } = await import('node:path')

    // Hand-rolled walk rather than `fs.globSync`, which only exists on Node 22+
    // — CI pins Node 20, where it is `undefined` and this test throws
    // `TypeError: globSync is not a function`. Same shape as the walk in
    // `scripts/i18n-codemod.mjs`.
    const walk = (dir: string, out: string[] = []): string[] => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, entry.name)
        if (entry.isDirectory()) walk(full, out)
        else if (entry.name.endsWith('.tsx')) out.push(full)
      }
      return out
    }

    const files = walk('src').filter(f => !f.includes('.test.'))
    // A walk that silently resolved nothing would make this guard vacuous — it
    // would pass forever while scanning zero files. That is exactly the failure
    // mode `globSync` had here (undefined on Node 20), so the count is asserted.
    expect(files.length, 'no .tsx sources found — the walk is broken').toBeGreaterThan(100)

    const offenders: string[] = []
    // Matches confirm('…') / window.alert("…") with a literal containing a letter.
    const pattern = /\b(?:window\.)?(?:confirm|alert)\(\s*['"][^'"]*[A-Za-z][^'"]*['"]\s*\)/g
    for (const rel of files) {
      const src = readFileSync(rel, 'utf-8')
      for (const hit of src.match(pattern) ?? []) {
        offenders.push(`${rel}: ${hit.slice(0, 70)}`)
      }
    }
    expect(offenders, offenders.slice(0, 6).join('\n')).toEqual([])
  })
})

describe('literal safety tokens stay untranslated', () => {
  /**
   * A "type X to confirm" gate compares the user's input against a LITERAL
   * token. If that token is rendered from the catalog, the translated build shows
   * a word the comparison will never accept — the confirm button can never arm,
   * so the destructive action becomes unreachable in that language.
   *
   * Real instance: SchedulePage's bulk delete displayed 删除 while `confirmArmed`
   * required `'delete'`, so zh-CN users could not bulk-delete cron jobs at all.
   * Nothing else catches it: the code type-checks and the English UI works.
   *
   * The token now lives in one exported constant used by BOTH the comparison and
   * the rendered elements, so they cannot drift — and there is no bare literal
   * left for the i18n codemod to re-convert on a future run (it did exactly that
   * once, silently undoing the fix).
   */
  it('SchedulePage bulk-delete token is a shared constant, never a catalog lookup', async () => {
    const { readFileSync } = await import('node:fs')
    const src = readFileSync('src/pages/SchedulePage.tsx', 'utf-8')

    // One definition...
    expect(src, 'BULK_DELETE_TOKEN constant is missing')
      .toMatch(/export const BULK_DELETE_TOKEN = '[^']+'/)

    // ...used by the comparison, the instruction, and the placeholder.
    expect(src, 'comparison must use the shared constant')
      .toMatch(/confirmText\s*\.trim\(\)\s*\.toLowerCase\(\)\s*===\s*BULK_DELETE_TOKEN/)
    expect(src, 'instruction must render the constant, not i18nT()')
      .toMatch(/<code[^>]*>\{BULK_DELETE_TOKEN\}<\/code>/)
    expect(src, 'placeholder must be the constant, not i18nT()')
      .toContain('placeholder={BULK_DELETE_TOKEN}')

    // NOTE: deliberately no assertion that no catalog value equals "delete" —
    // `pages.schedulePage.delete` is the per-row Delete BUTTON label ("Delete N
    // selected"), which SHOULD be translated. The distinction that matters is
    // where a string is USED (compared vs displayed), not what it says, which is
    // exactly why this test pins the call sites rather than the catalog.
  })
})

describe('codemod writes sources and catalog atomically', () => {
  /**
   * The catalog shrink-guard is only safe if source rewrites are deferred until
   * after it passes. When sources were written first, a default re-run over an
   * already-converted tree with a few new literals would: rewrite those files to
   * call `i18nT('new.key')`, correctly refuse the tiny catalog, then exit —
   * leaving call sites pointing at keys that do not exist, which render as raw
   * key text in the UI. Reproduced exactly that way.
   *
   * This asserts the ordering structurally: the only `writeFileSync` to a source
   * file lives in `flushSourceWrites`, and every call to it is preceded by the
   * catalog write.
   */
  it('defers source writes until after the catalog is validated', async () => {
    const { readFileSync } = await import('node:fs')
    const src = readFileSync('scripts/i18n-codemod.mjs', 'utf-8')

    // processFile must QUEUE, not write.
    expect(src, 'processFile must queue rewrites, not write them')
      .toMatch(/pendingSourceWrites\.set\(file, out\)/)
    expect(src, 'no direct source write may remain in processFile')
      .not.toMatch(/fs\.writeFileSync\(file, out\)/)

    // Flush exists and is only reachable after a catalog write.
    expect(src).toMatch(/function flushSourceWrites\(\)/)
    const flushCalls = [...src.matchAll(/flushSourceWrites\(\)/g)]
      .filter(m => !src.slice(Math.max(0, m.index! - 20), m.index!).includes('function '))
    expect(flushCalls.length, 'flush must be called on both write paths').toBeGreaterThanOrEqual(2)
    for (const call of flushCalls) {
      const before = src.slice(0, call.index!)
      expect(before, 'flushSourceWrites() called before any catalog write')
        .toMatch(/writeFileSync\(EN_CATALOG/)
    }
  })
})
