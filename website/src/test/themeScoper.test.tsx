import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The runtime overrides.css scoper (_scopeOverridesCss / _selectorAllowed) is
// module-private in useTheme.tsx, so we exercise it end-to-end through the
// public path: an active installed theme with `assets.hasOverrides` triggers
// applyThemeOverrides, which fetches overrides.css, runs it through the
// positive-selector allowlist, and injects the surviving rules into
// <style id="mc-theme-overrides">. We mock fetch to serve a CSS fixture and
// assert which rules were kept vs dropped.

const themesFn = vi.fn()
const themeDetailFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => themesFn(),
    themeDetail: (slug: string) => themeDetailFn(slug),
    themeBoot: () => Promise.resolve({}),
    updateThemeConfig: () => Promise.resolve({}),
  },
}))

import { useTheme, ThemeProvider } from '../hooks/useTheme'

const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

/** Install one L1 theme ("scoped") carrying overrides.css, and select it. */
function seedScopedTheme() {
  themesFn.mockResolvedValue({
    themes: [{ slug: 'scoped', name: 'Scoped', emoji: '🎨', source: 'installed' }],
  })
  themeDetailFn.mockResolvedValue({
    slug: 'scoped',
    name: 'Scoped',
    emoji: '🎨',
    dark: {},
    light: {},
    level: 1,
    assets: { hasOverrides: true },
  })
  localStorage.setItem('mc-color-theme', 'custom-scoped')
}

/** Render the provider with a mocked fetch that serves `css` for overrides.css. */
async function injectedOverrides(css: string): Promise<string> {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    text: () => Promise.resolve(css),
  })
  ;(globalThis as unknown as { fetch: unknown }).fetch = fetchMock
  renderHook(() => useTheme(), { wrapper })
  await waitFor(() => {
    const el = document.getElementById('mc-theme-overrides')
    expect(el).toBeTruthy()
  })
  return document.getElementById('mc-theme-overrides')!.textContent || ''
}

describe('useTheme — runtime overrides.css positive-selector scoper (§4.2/§5.1)', () => {
  let origFetch: unknown
  beforeEach(() => {
    localStorage.clear()
    delete document.documentElement.dataset.theme
    document.getElementById('mc-theme-overrides')?.remove()
    origFetch = (globalThis as unknown as { fetch: unknown }).fetch
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    themesFn.mockReset()
    themeDetailFn.mockReset()
  })
  afterEach(() => {
    ;(globalThis as unknown as { fetch: unknown }).fetch = origFetch
  })

  it('keeps allowlisted rules (body::before, chained .topbar:hover, [data-theme] body)', async () => {
    seedScopedTheme()
    const css = [
      'body::before{--m:kbefore}',
      '.topbar.chained:hover{--m:ktopbar}',
      '[data-theme="y"] body{--m:kdata}',
    ].join('\n')
    const out = await injectedOverrides(css)
    expect(out).toContain('kbefore')
    expect(out).toContain('ktopbar')
    expect(out).toContain('kdata')
  })

  it('drops non-allowlisted rules (.random, #app-root, iframe, descendant combinator)', async () => {
    seedScopedTheme()
    const css = [
      'body{--m:kbody}', // one keeper so the <style> is injected
      '.random{--m:drand}',
      '#app-root{--m:dappid}',
      'iframe{--m:diframe}',
      'body .descend{--m:ddescend}',
    ].join('\n')
    const out = await injectedOverrides(css)
    expect(out).toContain('kbody')
    expect(out).not.toContain('drand')
    expect(out).not.toContain('dappid')
    expect(out).not.toContain('diframe')
    expect(out).not.toContain('ddescend')
  })

  it('recurses into @media (keeps inner body, drops inner iframe) and drops non-@media at-rules', async () => {
    seedScopedTheme()
    const css = [
      '@media (max-width: 500px){body{--m:kmediabody} iframe{--m:dmediaif}}',
      "@font-face{font-family:'zz';--m:dfont}",
      'body{--m:kplain}',
      '@import "evil.css";',
    ].join('\n')
    const out = await injectedOverrides(css)
    expect(out).toContain('@media')
    expect(out).toContain('kmediabody')
    expect(out).toContain('kplain')
    expect(out).not.toContain('dmediaif')
    expect(out).not.toContain('dfont')
    expect(out).not.toContain('evil.css')
  })

  it('rewrites a pack-relative url() in a kept rule to the absolute asset URL', async () => {
    seedScopedTheme()
    const css = "body::before{--m:kurl;background-image:url('../branding/x.png')}"
    const out = await injectedOverrides(css)
    expect(out).toContain('kurl')
    // ../branding/x.png resolved against styles/ → <assetBase>/branding/x.png
    expect(out).toContain("url('/api/theme/scoped/assets/branding/x.png')")
    expect(out).not.toContain('../branding')
  })

  it('leaves a data: url() untouched', async () => {
    seedScopedTheme()
    const css = 'body::before{--m:kdata;background-image:url(data:image/png;base64,AA)}'
    const out = await injectedOverrides(css)
    expect(out).toContain('kdata')
    expect(out).toContain('url(data:image/png;base64,AA)')
  })

  it('neutralizes a traversal url() so no raw ../ escapes the pack', async () => {
    seedScopedTheme()
    const css = "body::before{--m:ktrav;background-image:url('../../..//etc')}"
    const out = await injectedOverrides(css)
    expect(out).toContain('ktrav') // rule kept…
    expect(out).not.toContain('etc') // …but the traversal ref is gone
    expect(out).not.toContain('..')
  })

  it('counts braces only outside strings: a kept rule with content:"}" does not smuggle a following non-allowlisted rule', async () => {
    seedScopedTheme()
    // The `}` lives inside a CSS string. A brace-desynced walker would treat it
    // as the end of the body rule, mis-slice the stream, and let the following
    // `.random` rule ride along. The string-aware walker must keep body intact
    // and still DROP `.random`.
    const css = ["body::before{content:\"}\";--m:kbrace}", '.random{--m:dsmuggle}'].join('\n')
    const out = await injectedOverrides(css)
    expect(out).toContain('kbrace') // kept rule survives intact
    expect(out).toContain('content:"}"') // its string literal is preserved verbatim
    expect(out).not.toContain('dsmuggle') // non-allowlisted rule still dropped
    expect(out).not.toContain('.random')
  })
})
