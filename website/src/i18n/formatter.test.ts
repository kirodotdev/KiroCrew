/**
 * Gate for the i18next v26 runtime: is the built-in Formatter actually active?
 *
 * v26 deleted the legacy `interpolation.format` callback and made the built-in
 * Formatter mandatory. "Mandatory" is a claim about the library, not about this
 * app — a wrong `init()` could still leave `{{v, number}}` unformatted, and
 * nothing else in the suite would notice: catalog-parity tests read JSON, render
 * tests read plain strings, and no shipped key carries a format spec yet. So the
 * regression this file guards is invisible until locale-aware formatting lands on
 * top of it and starts producing English-shaped numbers in German.
 *
 * The assertions are therefore about the SEAM, not about any catalog value: the
 * formatter service exists, it is wired into interpolation, it reaches Intl, and
 * it follows `changeLanguage`.
 *
 * `integration/setup.ts` has already called `initI18n('en')` — i18next is a
 * module-level singleton, so this file switches language rather than re-initing,
 * and restores English afterwards like its sibling tests.
 */

import { describe, it, expect, beforeAll, afterEach } from 'vitest'

import { i18next } from './index'

/** Format specs live in a test-only bundle: no shipped key uses one yet. */
const FIXTURE_KEY = 'test.formatter.amount'
const FIXTURE_VALUE = 'Total {{n, number}}'
const AMOUNT = 1234.5

afterEach(async () => {
  await i18next.changeLanguage('en')
})

describe('i18next v26 built-in formatter', () => {
  beforeAll(() => {
    for (const lng of ['de', 'en']) {
      i18next.addResourceBundle(lng, 'translation', {
        test: { formatter: { amount: FIXTURE_VALUE } },
      }, true, true)
    }
  })

  it('exposes the formatter as an initialized service', () => {
    // v25 built this only when a formatter plugin was supplied; v26 always does.
    expect(i18next.services.formatter).toBeDefined()
  })

  it('wires the formatter into interpolation instead of an app-supplied callback', () => {
    // v26 sets `options.interpolation.format` ITSELF, from the built-in formatter
    // it just constructed. A function here is therefore the positive signal that
    // the built-in path is live — and since `initI18n` passes no `format` of its
    // own, it cannot be an app callback shadowing it.
    expect(typeof i18next.options.interpolation?.format).toBe('function')
  })

  it('renders {{n, number}} through Intl for the requested language', () => {
    // de groups with '.' and decimals with ',' — the inverse of en. Asserting the
    // German shape proves Intl ran with the German locale, not that a string
    // passed through untouched.
    //
    // Both sides derive from the same Intl, deliberately: a cross-locale
    // INEQUALITY assertion (de output !== en output) would hard-fail on a
    // small-icu Node, where every locale collapses to en-US. Official Node 20+
    // builds are full-icu, so this reads as the real German format there and
    // degrades to a vacuous pass elsewhere — the right failure mode for a wiring
    // test that is not meant to gate the runtime's ICU build.
    expect(i18next.t(FIXTURE_KEY, { n: AMOUNT, lng: 'de' }))
      .toBe(`Total ${new Intl.NumberFormat('de').format(AMOUNT)}`)
  })

  it('re-formats the same key after changeLanguage', async () => {
    // The failure this catches: a formatter pinned to the boot language. Every
    // later locale-aware format depends on the spec re-resolving per language.
    await i18next.changeLanguage('de')
    expect(i18next.t(FIXTURE_KEY, { n: AMOUNT })).toBe(`Total ${new Intl.NumberFormat('de').format(AMOUNT)}`)

    await i18next.changeLanguage('en')
    expect(i18next.t(FIXTURE_KEY, { n: AMOUNT })).toBe(`Total ${new Intl.NumberFormat('en').format(AMOUNT)}`)
  })
})
