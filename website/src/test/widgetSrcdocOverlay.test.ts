import { describe, it, expect } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'

const base = { themeVars: {}, mode: 'dark' as const }

describe('buildSrcdoc loading indicator', () => {
  it('omits the indicator by default', () => {
    const out = buildSrcdoc({ html: '<p>hi</p>', ...base })
    expect(out).not.toContain('mc-tw-loading')
  })

  it('injects the indicator and its reveal script when requested', () => {
    const out = buildSrcdoc({
      html: '<p>hi</p>', ...base, showLoadingOverlay: true, loadingLabel: 'Rendering…',
    })
    expect(out).toContain('id="mc-tw-loading"')
    expect(out).toContain('MutationObserver')
  })

  it('never assigns innerHTML — the indicator is built with DOM APIs', () => {
    // Blocking repo rule (`Automated Rule Check` -> "Never assign .innerHTML").
    // The label is caller-supplied text and must not reach an HTML parser sink.
    const out = buildSrcdoc({
      html: '<p>hi</p>', ...base, showLoadingOverlay: true, loadingLabel: 'Rendering…',
    })
    expect(out).not.toContain('innerHTML')
  })

  it('renders the caller-supplied label, with no hardcoded English fallback', () => {
    // The iframe cannot read the parent's i18n catalog, so a default baked in
    // here would visibly flip to English mid-load in every non-English locale.
    const out = buildSrcdoc({
      html: '<p>hi</p>', ...base, showLoadingOverlay: true, loadingLabel: '描画中…',
    })
    expect(out).toContain('描画中…')
    expect(out).not.toContain('Rendering')
  })

  it('escapes a label that contains markup instead of parsing it', () => {
    const out = buildSrcdoc({
      html: '<p>hi</p>', ...base, showLoadingOverlay: true,
      loadingLabel: '<img src=x onerror=alert(1)>',
    })
    expect(out).not.toContain('<img src=x')
    expect(out).toContain('&lt;img')
  })

  it('uses a long hang backstop, not a short compile budget', () => {
    // Regression guard: a 2s timeout uncovered the widget while it was still
    // unstyled for exactly the slow widgets this feature targets, reproducing
    // the blank-widget symptom. The observer is the normal path; the timeout
    // only rescues a hung runtime.
    const out = buildSrcdoc({
      html: '<p>hi</p>', ...base, showLoadingOverlay: true, loadingLabel: 'x',
    })
    expect(out).not.toContain('setTimeout(finish,2000)')
    expect(out).toContain('setTimeout(finish,15000)')
  })

  it('pins the indicator to the TOP, never centred', () => {
    // The height reporter grows the frame to the content height (measured up to
    // 1781px), so a centred indicator lands below the fold — mounted but
    // invisible, defeating its purpose.
    const out = buildSrcdoc({
      html: '<p>hi</p>', ...base, showLoadingOverlay: true, loadingLabel: 'x',
    })
    expect(out).toContain('align-items:flex-start')
    expect(out).not.toContain('align-items:center')
  })

  it('honours prefers-reduced-motion for the spinner', () => {
    const out = buildSrcdoc({
      html: '<p>hi</p>', ...base, showLoadingOverlay: true, loadingLabel: 'x',
    })
    expect(out).toContain('prefers-reduced-motion')
  })

  it('always loads the Tailwind runtime, indicator or not', () => {
    // Regression guard for the inverted-logic bug: the runtime must never be
    // dropped on the strength of a static class scan.
    for (const opts of [{}, { showLoadingOverlay: true, loadingLabel: 'x' }]) {
      const out = buildSrcdoc({ html: '<p>hi</p>', ...base, ...opts })
      expect(out).toMatch(/<script[^>]+src="[^"]*tailwindcss-browser\.js"/)
      expect(out).toContain('text/tailwindcss')
    }
  })
})
