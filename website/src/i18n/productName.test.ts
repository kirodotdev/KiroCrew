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

import { CATALOGS } from './catalogs'
import { initI18n, i18next, setProductName } from './all'

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

  it('no catalog value hardcodes the product name outside the documented exceptions', () => {
    // The regression this catches: an upstream-authored key lands with the
    // literal instead of the placeholder, and an edition's rebranding is
    // silently incomplete — nothing else fails, because placeholder parity
    // only compares placeholders that exist in en.
    //
    // The exceptions are the strings whose referent does NOT change with an
    // edition (see i18n-catalog.md "The product name is an interpolation
    // variable"): the apps.*.manifest.* mirror of the Python app.json prose,
    // and attribution/data-egress copy naming this project or the
    // hardcoded-upstream recipients of the survey and install receipts.
    const isManifestKey = (k: string) => /^apps\.[^.]+\.manifest\./.test(k)
    const EXCEPTIONS = new Set([
      'app.star_kirocrew_on_github',
      'components.sessionPulseSurveyCard.email_disclosure',
      'privacyDisclosure.installReceiptBody',
      'privacyDisclosure.installReceiptFields',
      // Wire-format identifiers, not brand prose: the generated Slack app name
      // and the webhook signature header names are fixed by the backend, so a
      // rebranded UI must still spell them exactly.
      'pages.settings.slackPanel.create_the_slack_app_from_the_manifest',
      'pages.webhooksPage.calls_must_send_signature_headers_detail',
    ])
    const flatten = (obj: unknown, prefix = ''): Record<string, string> => {
      const out: Record<string, string> = {}
      if (obj === null || typeof obj !== 'object') return out
      for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
        const path = prefix ? `${prefix}.${key}` : key
        if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
        else out[path] = String(value)
      }
      return out
    }
    for (const [lang, catalog] of Object.entries(CATALOGS)) {
      // The pseudolocale accents the literal, so it cannot carry it verbatim;
      // its freshness against en is the [pseudolocale] gate's job.
      if (lang === 'en-XA') continue
      const flat = flatten(catalog.translation)
      const offenders = Object.entries(flat)
        .filter(([k, v]) => !isManifestKey(k) && !EXCEPTIONS.has(k)
          // The joined form is scanned for as a defect here, never written as prose.
          && (v.includes('Kiro Crew') || v.includes('Kiro-Crew') || v.includes('KiroCrew'))) // brand-ok
        .map(([k]) => `${lang}:${k}`)
      expect(offenders, offenders.slice(0, 5).join(', ')).toEqual([])
      // The guard is bidirectional: an exception key that GAINS the placeholder
      // is the misattribution bug batch 2 shipped and this suite reverted — the
      // survey and receipt endpoints stay hardcoded upstream whatever the
      // edition renders elsewhere, so these strings must keep naming them.
      for (const k of EXCEPTIONS) {
        const v = flat[k]
        if (v === undefined) continue
        expect(v, `${lang}:${k} must keep the literal product name`).toMatch(/Kiro[ -]?Crew/) // brand-ok
        expect(v, `${lang}:${k} must not interpolate the product name`).not.toContain('{{productName}}')
      }
    }
  })
})
