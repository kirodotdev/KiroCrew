/**
 * No reassurance lines in English UI copy.
 *
 * A sentence like "Nothing is sent until you press send" or "Nothing changes
 * until you install" restates what the control already shows: the button is
 * right there, and pressing it is the only way anything happens. Such a line
 * costs a read and tells the reader nothing new. When an action has a real
 * consequence the reader cannot infer, the copy states that consequence
 * directly ("Billable.", "keeps running until you stop it in AWS").
 *
 * The rule and its examples: website/docs/i18n-catalog.md, "No reassurance
 * lines". Translations follow English, so the scan reads the English source.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

// "Nothing ... until you ...", "none of them ... until you ...", within one clause.
const REASSURANCE = /\b(?:nothing|none of (?:them|these|it))\b[^.!?;:。]{0,80}?\buntil\s+(?:you|your)\b/i

// Copy that belongs to a built-in app pinned in apps/adoption-pins.json. Its fix
// lands in the app's own repository first (AGENTS.md, "No built-in apps"); this
// list only shrinks.
const APP_REPO_OWNED = new Set([
  'apps.codeReviewSage.views.learningView.reviews_read_the_consolidated_ruleset_below_neve',
  'apps.projectScaffolder.manifest.highlight_1',
])

function strings(node: unknown, path: string, out: Array<[string, string]>): void {
  if (typeof node === 'string') out.push([path, node])
  else if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) strings(v, path ? `${path}.${k}` : k, out)
  }
}

const en = (CATALOGS as Record<string, { translation: Record<string, unknown> }>).en.translation

describe('English UI copy has no reassurance lines', () => {
  it('reads the English catalog', () => {
    const all: Array<[string, string]> = []
    strings(en, '', all)
    expect(all.length).toBeGreaterThan(1000)
  })

  it('has no "Nothing ... until you ..." sentence', () => {
    const all: Array<[string, string]> = []
    strings(en, '', all)
    const hits = all.filter(([k, v]) => REASSURANCE.test(v) && !APP_REPO_OWNED.has(k)).map(([k, v]) => `${k}: ${v}`)
    expect(hits).toEqual([])
  })

  it('keeps the app-owned list to keys that still match', () => {
    const all = new Map<string, string>()
    const pairs: Array<[string, string]> = []
    strings(en, '', pairs)
    for (const [k, v] of pairs) all.set(k, v)
    for (const key of APP_REPO_OWNED) expect(REASSURANCE.test(all.get(key) ?? ''), key).toBe(true)
  })

  it.each([
    ['Nothing is sent until you press send.', true],
    ['Nothing changes until you install.', true],
    ['The guide ends here; nothing is added until you save the form yourself.', true],
    ['None of them run until you approve them here.', true],
    ['Removing or toggling changes nothing until you save.', true],
    ['The EC2 instance keeps running and billing until you delete it in AWS.', false],
    ['Nothing to show. Pick a folder until you find one.', false],
  ])('matches %j: %s', (text, expected) => {
    expect(REASSURANCE.test(text)).toBe(expected)
  })
})
