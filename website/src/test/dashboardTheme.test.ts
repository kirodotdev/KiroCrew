import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  adoptDashboardTheme,
  applyThemeId,
  extractStylesheetHrefs,
} from '../apps/crew-companion/dashboardTheme'

/**
 * Pins the fix for the crew-companion overlay adopting the dashboard theme:
 *  - the RIGHT stylesheet(s) are discovered (root cause of the fallback #2a2a2a menu),
 *  - `data-theme` is copied so variables resolve to the user's theme, not :root light,
 *  - window transparency survives adopting the dashboard's body-painting stylesheet.
 */

function cleanup() {
  // Drop anything the module injected + reset the attributes it writes.
  document.head.querySelectorAll('link[data-cc-dashboard-theme]').forEach((l) => l.remove())
  document.getElementById('cc-window-transparent')?.remove()
  delete document.documentElement.dataset.theme
  delete document.documentElement.dataset.mode
  document.documentElement.removeAttribute('style')
  document.body.removeAttribute('style')
  localStorage.clear()
}

beforeEach(cleanup)
afterEach(() => {
  vi.unstubAllGlobals()
  cleanup()
})

describe('extractStylesheetHrefs', () => {
  // The shape Vite actually emits into the built index.html: rel/crossorigin
  // BEFORE href, code-split into several content-hashed chunks. The theme
  // variables live in the `src-*.css` chunk, not the `main-*.css` entry chunk.
  const builtHtml = `<!DOCTYPE html><html lang="en" data-theme="dark"><head>
    <link rel="stylesheet" crossorigin href="/assets/vendor-markdown-CaH0jbfC.css">
    <link rel="stylesheet" crossorigin href="/assets/App-9RQJ1adl.css">
    <link rel="stylesheet" crossorigin href="/assets/main-CCVejdd_.css">
    <link rel="stylesheet" crossorigin href="/assets/src-BZQnxrA2.css">
  </head><body></body></html>`

  it('returns every linked stylesheet, including the theme chunk', () => {
    const hrefs = extractStylesheetHrefs(builtHtml)
    expect(hrefs).toEqual([
      '/assets/vendor-markdown-CaH0jbfC.css',
      '/assets/App-9RQJ1adl.css',
      '/assets/main-CCVejdd_.css',
      '/assets/src-BZQnxrA2.css',
    ])
  })

  it('does NOT drop the theme chunk in favour of only the first sheet (the old bug)', () => {
    // The previous name heuristic looked for `/index-*.css`, found none, and fell
    // back to the FIRST href (vendor-markdown), which defines no theme variables.
    const hrefs = extractStylesheetHrefs(builtHtml)
    expect(hrefs).toContain('/assets/src-BZQnxrA2.css')
    expect(hrefs.length).toBeGreaterThan(1)
  })

  it('de-duplicates repeated hrefs (e.g. preload + stylesheet)', () => {
    const html = `<link rel="preload" as="style" href="/assets/src-x.css">
                  <link rel="stylesheet" href="/assets/src-x.css">`
    expect(extractStylesheetHrefs(html)).toEqual(['/assets/src-x.css'])
  })

  it('returns an empty array when there are no stylesheet links', () => {
    expect(extractStylesheetHrefs('<html><head></head><body></body></html>')).toEqual([])
  })
})

describe('applyThemeId — copies the dashboard theme selection onto <html>', () => {
  it('maps a built-in theme + explicit dark mode to `<name>-dark`', () => {
    localStorage.setItem('mc-color-theme', 'monokai')
    localStorage.setItem('mc-theme', 'dark')
    applyThemeId()
    expect(document.documentElement.dataset.theme).toBe('monokai-dark')
    expect(document.documentElement.dataset.mode).toBe('dark')
  })

  it('maps the emerald default to the bare mode id (no prefix)', () => {
    localStorage.setItem('mc-color-theme', 'emerald')
    localStorage.setItem('mc-theme', 'light')
    applyThemeId()
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(document.documentElement.dataset.mode).toBe('light')
  })

  it('maps a custom theme value to `custom-<slug>-<mode>`', () => {
    localStorage.setItem('mc-color-theme', 'custom-my-brand')
    localStorage.setItem('mc-theme', 'dark')
    applyThemeId()
    expect(document.documentElement.dataset.theme).toBe('custom-my-brand-dark')
  })

  it('resolves `system` preference through prefers-color-scheme: dark', () => {
    localStorage.setItem('mc-color-theme', 'kiro')
    localStorage.setItem('mc-theme', 'system')
    window.matchMedia = vi.fn().mockReturnValue({ matches: true }) as unknown as typeof window.matchMedia
    applyThemeId()
    expect(document.documentElement.dataset.theme).toBe('kiro-dark')
    expect(document.documentElement.dataset.mode).toBe('dark')
  })

  it('falls back to the kiro default when nothing is persisted', () => {
    window.matchMedia = vi.fn().mockReturnValue({ matches: false }) as unknown as typeof window.matchMedia
    applyThemeId()
    // kiro + system(light) → kiro-light
    expect(document.documentElement.dataset.theme).toBe('kiro-light')
  })
})

describe('adoptDashboardTheme — transparency survives adopting the dashboard CSS', () => {
  it('forces html/body transparent and neutralizes the body::before gradient', async () => {
    // No stylesheet found → adopt still runs the transparency guard and returns
    // without injecting a <link> (so the test never waits on a real sheet load).
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))

    await adoptDashboardTheme()

    for (const el of [document.documentElement, document.body]) {
      // happy-dom re-serializes the `background` shorthand from its longhands
      // (→ "none transparent"), so assert the transparency intent, not the exact string.
      expect(el.style.getPropertyValue('background')).toContain('transparent')
      expect(el.style.getPropertyValue('background-image')).toBe('none')
      expect(el.style.getPropertyPriority('background-image')).toBe('important')
    }

    const guard = document.getElementById('cc-window-transparent')
    expect(guard).not.toBeNull()
    // The guard must be the LAST child of <head> so it outranks the dashboard sheet.
    expect(document.head.lastElementChild).toBe(guard)
    /*
     * Asserted on the DECLARATIONS, not on their character-for-character spelling.
     * The rules live in a real stylesheet now, so they carry ordinary CSS whitespace;
     * matching `display:none !important` verbatim made this test fail on a formatting
     * change while the behaviour was identical, which is a false alarm, not a guard.
     */
    const css = (guard!.textContent ?? '').replace(/\s+/g, '')
    expect(css).toContain('body::before')
    expect(css).toContain('display:none!important')
    expect(css).toContain('background:transparent!important')
    expect(css).toContain('content:none!important')
  })

  it('injects EVERY dashboard stylesheet, not just the first one', async () => {
    const html = `<html data-theme="dark"><head>
      <link rel="stylesheet" crossorigin href="/assets/vendor-markdown-a.css">
      <link rel="stylesheet" crossorigin href="/assets/main-b.css">
      <link rel="stylesheet" crossorigin href="/assets/src-c.css">
    </head><body></body></html>`
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, text: () => Promise.resolve(html) }),
    )

    const promise = adoptDashboardTheme()
    // Wait for fetch+text microtasks to settle and the links to be appended.
    await vi.waitFor(() => {
      expect(document.head.querySelectorAll('link[data-cc-dashboard-theme]').length).toBe(3)
    })
    // Fire load so the adopt promise resolves regardless of happy-dom's loader.
    document.head
      .querySelectorAll('link[data-cc-dashboard-theme]')
      .forEach((l) => l.dispatchEvent(new Event('load')))
    await promise

    const hrefs = [...document.head.querySelectorAll('link[data-cc-dashboard-theme]')].map((l) =>
      (l as HTMLLinkElement).getAttribute('href'),
    )
    expect(hrefs).toContain('/assets/src-c.css') // the theme chunk
    expect(hrefs).toContain('/assets/main-b.css')
    expect(hrefs).toContain('/assets/vendor-markdown-a.css')
    // Transparency guard still sits last, after all injected links.
    expect(document.head.lastElementChild?.id).toBe('cc-window-transparent')
  })

  it('is idempotent — a second call does not double-inject', async () => {
    const html = `<head><link rel="stylesheet" href="/assets/src-c.css"></head>`
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, text: () => Promise.resolve(html) }),
    )

    const first = adoptDashboardTheme()
    await vi.waitFor(() =>
      expect(document.head.querySelectorAll('link[data-cc-dashboard-theme]').length).toBe(1),
    )
    document.head
      .querySelectorAll('link[data-cc-dashboard-theme]')
      .forEach((l) => l.dispatchEvent(new Event('load')))
    await first

    await adoptDashboardTheme()
    expect(document.head.querySelectorAll('link[data-cc-dashboard-theme]').length).toBe(1)
  })
})
