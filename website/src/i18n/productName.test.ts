/**
 * The `{{productName}}` contract.
 *
 * Catalog values will interpolate the product name instead of hardcoding it,
 * so a downstream edition can rebrand by overriding one variable from its
 * composition root instead of forking every locale file. These tests pin the
 * three properties the arrangement rests on: the stock default renders the
 * exact same English a hardcoded literal would, the variable is wired as an
 * i18next `defaultVariables` (so it survives the planned lazy-catalog
 * migration untouched), and a call-time variable still wins.
 *
 * The tests interpolate against an injected resource rather than a shipped
 * catalog key: the mechanical catalog rewrite lands in follow-up PRs (the
 * full-catalog diff exceeds the reviewable size limit), and this contract
 * must hold independently of how much of the catalog has been converted.
 * The catalog-wide "no non-manifest value hardcodes the literal" invariant
 * ships with the final rewrite chunk, where it can actually pass.
 */

import { describe, it, expect } from 'vitest'

import { initI18n, i18next, setProductName } from './index'

// No-op — the vitest setup file already initialized i18n. Explicit so this
// file also works standalone, and so the late-override test below is
// self-evidently running against an initialized instance.
initI18n()

// A value shaped exactly like the rewritten catalog strings will be. Injected
// under a test-only key so the assertion is independent of the rewrite's
// progress through the real catalogs.
i18next.addResource('en', 'translation', 'test.updating_product', 'Updating {{productName}}…')

describe('productName interpolation variable', () => {
  it('defaults to the stock product name', () => {
    expect(i18next.options.interpolation?.defaultVariables).toMatchObject({
      productName: 'Kiro Crew',
    })
  })

  it('renders a placeholder-bearing value identically to the old literal', () => {
    expect(i18next.t('test.updating_product')).toBe('Updating Kiro Crew…')
  })

  it('lets a call-time variable win over the default', () => {
    expect(i18next.t('test.updating_product', { productName: 'Acme' })).toBe('Updating Acme…')
  })

  it('keeps the update-restart handoff copy rebrandable', () => {
    const copy = i18next.t('pages.settings.aboutPanel.installing_quiet_note', { productName: 'Acme' })
    expect(copy).toContain('Acme')
    expect(copy).not.toContain('Kiro Crew')
  })

  it('refuses a late override rather than half-applying it', () => {
    // After init the variable has been handed to i18next; silently accepting
    // the call would leave the UI unchanged while the caller believes it
    // rebranded. Vitest runs with import.meta.env.DEV true, so this throws.
    expect(() => setProductName('Acme')).toThrow(/before initI18n/)
  })
})
