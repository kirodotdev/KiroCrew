/**
 * The diff-scoped catalog QA gate.
 *
 * `qa.test.ts` holds the frozen debt as a per-check COUNT, which cannot tell "one
 * fixed" from "one fixed and one broken". `changedValueFindings` is what closes that:
 * anything a branch adds or edits is checked at zero tolerance, with no stored ledger
 * and therefore nothing for two branches to conflict on. The whole argument for
 * relaxing the counts rests on this function, so its scoping rules are asserted here
 * rather than inferred from the gate's output.
 */

import { describe, it, expect } from 'vitest'

import { CHECKS, changedValueFindings } from '../../scripts/lib/qa-checks.mjs'

type Finding = { lang: string; key: string; value: string; check: { id: string } }

const ids = (f: Finding[]) => f.map((x) => x.check.id).sort()
const keys = (f: Finding[]) => f.map((x) => `${x.lang}:${x.key}`).sort()

describe('changedValueFindings — scoping', () => {
  it('ignores a value that did not change, however malformed', () => {
    // The frozen debt. This is precisely what the ceiling in `qa.test.ts` covers, and
    // checking it here would fail every branch on arrival.
    const frozen = { 'a.b': 'Skills (' }
    expect(changedValueFindings({ lang: 'en', base: frozen, head: frozen })).toEqual([])
  })

  it('flags a malformed value that is NEW on this branch', () => {
    const found = changedValueFindings({ lang: 'en', base: {}, head: { 'a.b': 'Skills (' } })
    expect(ids(found)).toEqual(['unbalanced-delimiter'])
  })

  it('flags a malformed value that was EDITED on this branch', () => {
    // The regression the count-based ceiling cannot see: the site is not new, but the
    // copy is, so there is no frozen debt to appeal to.
    const found = changedValueFindings({
      lang: 'en',
      base: { 'a.b': 'Skills' },
      head: { 'a.b': 'Skills (' },
    })
    expect(ids(found)).toEqual(['unbalanced-delimiter'])
  })

  it('accepts an edit that fixes the value', () => {
    const found = changedValueFindings({
      lang: 'en',
      base: { 'a.b': 'Skills (' },
      head: { 'a.b': 'Skills' },
    })
    expect(found).toEqual([])
  })

  it('reports every check a single value trips', () => {
    const found = changedValueFindings({ lang: 'en', base: {}, head: { k: ' Skills (  x' } })
    expect(ids(found)).toEqual(['doubled-space', 'edge-whitespace', 'unbalanced-delimiter'])
  })
})

describe('changedValueFindings — inherited defects are not the translator’s bug', () => {
  it('exempts a translation whose ENGLISH source trips the same check', () => {
    // `'Skills ('` cannot be translated into balanced Russian. Counting it against the
    // translator would block legitimate translation of every frozen fragment, which is
    // the measured failure mode recorded in translateDriver.test.ts.
    const found = changedValueFindings({
      lang: 'ru',
      base: {},
      head: { 'a.b': 'Навыки (' },
      enHead: { 'a.b': 'Skills (' },
    })
    expect(found).toEqual([])
  })

  it('still flags a translation when the English source is clean', () => {
    const found = changedValueFindings({
      lang: 'ru',
      base: {},
      head: { 'a.b': 'Навыки (' },
      enHead: { 'a.b': 'Skills' },
    })
    expect(keys(found)).toEqual(['ru:a.b'])
  })

  it('exempts only the check the English actually trips, not the whole value', () => {
    // English is unbalanced; the translation is unbalanced AND has edge whitespace. The
    // bracket is inherited, the whitespace is not.
    const found = changedValueFindings({
      lang: 'ru',
      base: {},
      head: { 'a.b': 'Навыки ( ' },
      enHead: { 'a.b': 'Skills (' },
    })
    expect(ids(found)).toEqual(['edge-whitespace'])
  })

  it('gives English itself no exemption', () => {
    // Otherwise every English value would exempt itself.
    const found = changedValueFindings({
      lang: 'en',
      base: {},
      head: { 'a.b': 'Skills (' },
      enHead: { 'a.b': 'Skills (' },
    })
    expect(ids(found)).toEqual(['unbalanced-delimiter'])
  })

  it('flags a translation for a key with no English source at all', () => {
    // A key present only in a non-English catalog is a separate defect that
    // `catalogParity` fails on, but it must not silently escape the QA checks here.
    const found = changedValueFindings({ lang: 'ru', base: {}, head: { orphan: 'x (' }, enHead: {} })
    expect(ids(found)).toEqual(['unbalanced-delimiter'])
  })
})

describe('changedValueFindings — locale-aware predicates survive the move to a module', () => {
  it('accepts correct German low-high quotes and rejects the English pair', () => {
    const de = (v: string) =>
      changedValueFindings({ lang: 'de', base: {}, head: { k: v } }).map((f) => f.check.id)
    expect(de('drücken Sie „Weiter“, um fortzufahren')).toEqual([])
    expect(de('drücken Sie “Weiter” hier')).toEqual(['odd-quote-count'])
  })

  it('rejects fullwidth Latin in a CJK value', () => {
    const found = changedValueFindings({ lang: 'zh-CN', base: {}, head: { k: 'ＫiroＣrew' } })
    expect(ids(found)).toEqual(['fullwidth-alphanumeric'])
  })

  it('exposes exactly the six shared checks', () => {
    // The count is the coupling between this gate and `qa.test.ts`'s CEILINGS map: a
    // seventh check needs a ceiling there, and that file asserts the reverse direction.
    expect(CHECKS.map((c: { id: string }) => c.id)).toEqual([
      'unbalanced-delimiter',
      'odd-quote-count',
      'edge-whitespace',
      'doubled-space',
      'bare-connector',
      'fullwidth-alphanumeric',
    ])
  })
})
