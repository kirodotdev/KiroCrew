import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// Shared-corpus guard for the two theme-CSS parsers (PR #107 arbiter item):
// this file asserts the `runtimeKeeps` column of the SAME fixture the backend
// asserts `installAccepts` against (test/test_theme_install.py
// ::TestCssParserCorpus), so any future parser drift fails a test, not a user.
// The parsers differ BY DESIGN (install = denylist, runtime = positive
// allowlist and the enforced boundary); the corpus pins both verdicts.

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

interface CorpusCase {
  name: string
  css: string
  installAccepts: boolean
  runtimeKeeps: boolean
}

const corpus: CorpusCase[] = JSON.parse(
  readFileSync(resolve(__dirname, '../../../test/fixtures/theme_css_corpus.json'), 'utf-8'),
).cases

const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

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

describe('theme CSS parser corpus — runtime scoper column (shared with backend)', () => {
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

  // Each corpus rule carries a unique `--m:cN` marker; the scoper keeps or
  // drops whole rules by selector, so marker presence == rule survived.
  // A constant keeper rule guarantees the <style> element mounts even for
  // cases whose own rule is dropped.
  for (const c of corpus) {
    it(`${c.name}: runtimeKeeps=${c.runtimeKeeps}`, async () => {
      seedScopedTheme()
      const marker = /--m:(c\d+)/.exec(c.css)?.[1]
      expect(marker, `corpus case ${c.name} must carry a --m:cN marker`).toBeTruthy()
      const out = await injectedOverrides(`body{--m:keeper}\n${c.css}`)
      expect(out).toContain('keeper')
      if (c.runtimeKeeps) {
        expect(out, `${c.name} should be KEPT by the runtime scoper`).toContain(marker!)
      } else {
        expect(out, `${c.name} should be DROPPED by the runtime scoper`).not.toContain(marker!)
      }
    })
  }

  it('corpus documents at least one install/runtime divergence', () => {
    expect(corpus.some((c) => c.installAccepts && !c.runtimeKeeps)).toBe(true)
  })
})
