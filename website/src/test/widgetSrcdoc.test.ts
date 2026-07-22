import { describe, it, expect } from 'vitest'
import { buildSrcdoc, THEME_VAR_NAMES } from '../lib/widgetSrcdoc'

describe('widgetSrcdoc', () => {
  it('embeds the html body', () => {
    const out = buildSrcdoc({
      html: '<p>hello world</p>',
      themeVars: {},
      mode: 'dark',
    })
    expect(out).toContain('<p>hello world</p>')
  })

  it('applies the dark mode class', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'dark' })
    expect(out).toContain('<body class="dark">')
  })

  it('applies the light mode class', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'light' })
    expect(out).toContain('<body class="light">')
  })

  it('serializes theme vars into a :root style block', () => {
    const out = buildSrcdoc({
      html: '<x/>',
      themeVars: { '--bg': '#000', '--text': '#fff' },
      mode: 'dark',
    })
    expect(out).toMatch(/--bg:#000/)
    expect(out).toMatch(/--text:#fff/)
    expect(out).toContain('color-scheme:dark')
  })

  it('omits the height reporter by default', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'light' })
    expect(out).not.toContain('mc-widget-height')
  })

  it('includes the height reporter when requested', () => {
    const out = buildSrcdoc({
      html: '',
      themeVars: {},
      mode: 'light',
      includeHeightReporter: true,
    })
    expect(out).toContain('mc-widget-height')
    expect(out).toContain('mc-widget-action')
  })

  it('loads Tailwind same-origin (no public CDN) with v4 dark-mode directives', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'dark' })
    // Locked-down-network fix: must NOT fetch Tailwind from the public CDN.
    expect(out).not.toContain('cdn.tailwindcss.com')
    // v4 drives dark: off a .dark class via a custom variant, not tailwind.config.
    expect(out).toContain('@custom-variant dark')
    expect(out).toContain('text/tailwindcss')
    // The directives block must precede the runtime <script> so the custom dark
    // variant registers before first paint (ordering regression guard).
    expect(out.indexOf('text/tailwindcss')).toBeLessThan(
      out.indexOf(`src="${window.location.origin}/vendor/tailwindcss-browser.js"`),
    )
    // The null-origin iframe can't use a bare path — the runtime <script> src
    // must be absolute (origin-prefixed), not just '/vendor/...'.
    expect(out).toContain(`src="${window.location.origin}/vendor/tailwindcss-browser.js"`)
  })

  it('sets the strict CSP', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'dark' })
    expect(out).toContain("default-src 'none'")
    expect(out).toContain("connect-src 'none'")
    expect(out).toContain("base-uri 'none'")
    // 'unsafe-eval' MUST NOT be granted anywhere: the Tailwind v4 runtime needs
    // no eval and widget JS must get no dynamic-exec primitive in the sandbox.
    expect(out).not.toContain("'unsafe-eval'")
    // script-src pins the dashboard origin to the single vendored runtime FILE
    // (least-privilege), not the whole origin (a null-origin iframe can't use
    // 'self'). Assert the exact path-scoped source precedes the jsdelivr host.
    expect(out).toContain(
      `'unsafe-inline' ${window.location.origin}/vendor/tailwindcss-browser.js https://cdn.jsdelivr.net`,
    )
  })

  it('exports the canonical theme variable names', () => {
    expect(THEME_VAR_NAMES).toContain('--bg')
    expect(THEME_VAR_NAMES).toContain('--text')
    expect(THEME_VAR_NAMES).toContain('--accent')
    expect(THEME_VAR_NAMES).toContain('--danger')
  })
})
