import { describe, it, expect } from 'vitest'
import * as fs from 'fs'
import * as path from 'path'

/**
 * The palette provider labels must stay ACCESSORS.
 *
 * Every provider's display label is a catalog lookup, and every provider object
 * is built inside a `useMemo` whose deps do not include the active language. A
 * plain `label: i18nT(PROVIDER_LABEL_KEY)` therefore resolves once and keeps the
 * boot language forever: switch language in Settings → Display, open the palette,
 * and the scope tab strip plus the Tab-completion hint still read `All` /
 * `Sessions` / `Pages` in the old locale while the rest of the palette translated.
 *
 * The reason a memo is fatal here — and the reason a plain call looks safe — is a
 * detail worth stating: `LanguageProvider` forces a re-RENDER of the tree on a
 * language change (`cloneElement`, which defeats React's referential-equality
 * bailout) but deliberately does NOT remount, because remounting would discard
 * in-flight state. A re-render re-runs render functions, so `i18nT()` in render
 * position re-resolves — but it does not recompute a memo. Several comments in
 * this repo have claimed `main.tsx` keys `<App>` on the language and so remounts;
 * `main.tsx` contains no `key=` at all.
 *
 * This is a SOURCE-shape assertion rather than a behavioural one on purpose: the
 * nine factories take different dependency shapes, and the defect is textual and
 * one line wide. Reverting any single getter is exactly the regression this
 * catches, and it caught nothing before this test existed — the frozen labels
 * shipped through a full green run of every gate and all 7100+ tests.
 */

const PROVIDERS_DIR = path.join(__dirname, 'providers')

/**
 * `recentsProvider` is exempt, and not merely because it is out of scope.
 * Its provider-level `label` has NO read site: `recents` is absent from the
 * palette's `tabs` list and is never handed to `registerProvider`, so the value
 * is inert. If it ever joins the tab strip, delete this exemption first.
 */
const EXEMPT = new Set(['recentsProvider.ts'])

/**
 * Comments are stripped before the negative assertion: the providers' own
 * explanatory comments quote the bad shape (`label: i18nT(...)`) verbatim to say
 * why it is wrong, and a naive match on the raw source flags that prose as the
 * defect it warns about.
 */
function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
}

function providerSources(): Array<{ file: string; src: string }> {
  return fs.readdirSync(PROVIDERS_DIR)
    .filter(f => f.endsWith('.ts') && !f.endsWith('.test.ts'))
    .filter(f => !EXEMPT.has(f))
    .map(f => ({ file: f, src: fs.readFileSync(path.join(PROVIDERS_DIR, f), 'utf-8') }))
    .filter(({ src }) => src.includes('PROVIDER_LABEL_KEY'))
}

describe('palette provider labels are getters, not frozen strings', () => {
  it('finds every provider that carries a label key', () => {
    // Guards the guard: a rename that emptied the glob would make the suite below
    // vacuously pass.
    expect(providerSources().length).toBeGreaterThanOrEqual(9)
  })

  for (const { file, src } of providerSources()) {
    it(`${file}: exposes label as an accessor`, () => {
      expect(src).toMatch(/get label\(\)\s*\{\s*return i18nT\(PROVIDER_LABEL_KEY\)\s*\}/)
    })

    it(`${file}: never assigns a resolved label`, () => {
      // The exact shape that froze nine tab labels at the boot language.
      expect(stripComments(src)).not.toMatch(/label:\s*i18nT\(/)
    })
  }
})
