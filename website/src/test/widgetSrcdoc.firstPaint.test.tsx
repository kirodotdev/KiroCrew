/** Does the emitted document theme itself BEFORE the parser-blocking script? */
import { describe, it, expect, vi } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

const VARS = { '--bg': '#0b1220', '--text': 'rgb(240,240,240)', '--card': '#111827' }

describe('first-paint theming', () => {
  it('puts the theme style before the runtime script', () => {
    const doc = buildSrcdoc({ html: '<p>hi</p>', themeVars: VARS, mode: 'dark' })
    const style = doc.indexOf('color-scheme:dark')
    const script = doc.indexOf('<script src=')
    expect(style).toBeGreaterThan(-1)
    expect(script).toBeGreaterThan(-1)
    // A head <script src> blocks parsing of everything after it. With the theme
    // behind it the browser paints its default WHITE canvas until the script
    // lands — a white flash on every open, worst on a phone over a slow link.
    expect(style).toBeLessThan(script)
  })

  it('emits no theme at all when no vars are readable', () => {
    // Deliberately NOT hardened with a color-scheme here: inventing a theme when
    // none was readable is worse than the browser's own defaults, and a sibling
    // guard in WidgetFrame.test pins that this path emits no `:root`.
    const doc = buildSrcdoc({ html: '<p>hi</p>', themeVars: {}, mode: 'dark' })
    expect(doc).not.toMatch(/:root/)
  })

  it('backgrounds the root element, not only the body', () => {
    const doc = buildSrcdoc({ html: '<p>hi</p>', themeVars: VARS, mode: 'dark' })
    // Body-only means an LLM body that sets its own background paints over the
    // browser's white canvas rather than over a themed base.
    expect(doc).toMatch(/html\{background:var\(--bg\)\}/)
  })

  it('still themes the body and keeps the vars', () => {
    const doc = buildSrcdoc({ html: '<p>hi</p>', themeVars: VARS, mode: 'dark' })
    expect(doc).toContain('--bg:#0b1220')
    expect(doc).toMatch(/body\{background:var\(--bg\);color:var\(--text\)\}/)
  })
})
