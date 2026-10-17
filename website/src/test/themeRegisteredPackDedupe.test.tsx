import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/**
 * An installed pack must not add a SECOND picker row for a theme an edition
 * already contributed via `registerTheme()`.
 *
 * `registerTheme()` de-duplicates against `THEMES` and `REGISTERED_THEMES`, but an
 * installed pack arrives asynchronously from `GET /api/themes` — long after
 * registration — so it cannot be caught there. The two also carry different
 * `value`s (`lcars` vs `custom-lcars`), which is why nothing flagged the overlap.
 *
 * The pack row is the broken one, and that asymmetry is the whole reason
 * registration wins: a registered theme's CSS is keyed to
 * `[data-theme="lcars-dark"]` and ships in the edition's compiled stylesheet,
 * while a pack renders under `[data-theme="custom-lcars-dark"]` — a selector that
 * stylesheet does not define — so the pack copy shows only the flat variables in
 * its `variables.json` and loses every structural rule (nav shapes, overlays,
 * message bubbles). Two rows, one of which is a near-unstyled decoy.
 */

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

import { useTheme, ThemeProvider, registerTheme } from '../hooks/useTheme'

const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

/** Serve `slugs` from GET /api/themes with the given source. */
function seedInstalledPacks(slugs: string[], source: 'installed' | 'editor' = 'installed') {
  themesFn.mockResolvedValue({
    themes: slugs.map(slug => ({ slug, name: slug, emoji: '🎨', source })),
  })
  themeDetailFn.mockImplementation((slug: string) =>
    Promise.resolve({
      slug,
      name: slug,
      emoji: '🎨',
      dark: { '--bg': '#000' },
      light: { '--bg': '#fff' },
      level: 0,
      assets: {},
    }),
  )
}

describe('allThemes — an installed pack never duplicates a registered theme', () => {
  beforeEach(() => {
    localStorage.clear()
    themesFn.mockReset()
    themeDetailFn.mockReset()
  })

  it('drops the pack row when its slug matches a registered theme', async () => {
    // The edition registers the theme (build time), and a pack with the SAME slug
    // is also installed on disk (the shape that produced duplicate rows).
    registerTheme([{ value: 'dedupe-lcars', label: '🖖 LCARS' }])
    seedInstalledPacks(['dedupe-lcars'])

    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(themeDetailFn).toHaveBeenCalled())

    await waitFor(() => {
      const rows = result.current.allThemes.filter(
        t => t.value === 'dedupe-lcars' || t.value === 'custom-dedupe-lcars',
      )
      // Exactly ONE row, and it is the registered one that owns the stylesheet.
      expect(rows.map(r => r.value)).toEqual(['dedupe-lcars'])
    })
  })

  it('keeps an installed pack that no registered theme shadows', async () => {
    // The guard must be surgical: an ordinary user pack is untouched. Without
    // this, "fixing" the duplicate by dropping installed packs wholesale would
    // silently remove every third-party theme from the picker.
    seedInstalledPacks(['dedupe-mine-only'])

    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(themeDetailFn).toHaveBeenCalled())

    await waitFor(() => {
      expect(result.current.allThemes.map(t => t.value)).toContain('custom-dedupe-mine-only')
    })
  })

  it('keeps an EDITOR-created theme that shares a registered slug', async () => {
    // Scope matters: an editor-created custom is the user's own object, and the
    // editor's edit/delete affordances are keyed off this list. Filtering it would
    // hide it with no way to reach or remove it — worse than a duplicate row.
    registerTheme([{ value: 'dedupe-mine', label: '🎨 Mine' }])
    seedInstalledPacks(['dedupe-mine'], 'editor')

    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(themeDetailFn).toHaveBeenCalled())

    await waitFor(() => {
      expect(result.current.allThemes.map(t => t.value)).toContain('custom-dedupe-mine')
    })
  })

  it('migrates a persisted selection off a shadowed pack to the registered theme', async () => {
    // Without this the dedupe strands the user: the pack is still installed (so
    // the uninstalled-pack repair never fires) but its row is filtered out, so
    // they keep the near-unstyled rendering with NO selected row in the picker.
    // Migrating beats resetting to the default — it is the same theme, styled.
    registerTheme([{ value: 'dedupe-stranded', label: '🖖 Stranded' }])
    seedInstalledPacks(['dedupe-stranded'])
    localStorage.setItem('mc-color-theme', 'custom-dedupe-stranded')

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(result.current.colorTheme).toBe('dedupe-stranded'))
    // And the selection now names a row that actually exists in the picker.
    expect(result.current.allThemes.map(t => t.value)).toContain('dedupe-stranded')
  })

  it('does not migrate a selection off an editor-created theme', async () => {
    // The editor theme is still in the picker, so migrating would silently take
    // the user off their own theme.
    registerTheme([{ value: 'dedupe-keepmine', label: '🎨 KeepMine' }])
    seedInstalledPacks(['dedupe-keepmine'], 'editor')
    localStorage.setItem('mc-color-theme', 'custom-dedupe-keepmine')

    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(themeDetailFn).toHaveBeenCalled())

    await waitFor(() => {
      expect(result.current.allThemes.map(t => t.value)).toContain('custom-dedupe-keepmine')
    })
    expect(result.current.colorTheme).toBe('custom-dedupe-keepmine')
  })

  it('does not drop a pack whose slug merely CONTAINS a registered value', async () => {
    // Matching must be on the whole slug, not a prefix/substring: a registered
    // `dedupe-kr` must not evict an unrelated `dedupe-kr-extended` pack.
    registerTheme([{ value: 'dedupe-kr', label: '🚗 KR' }])
    seedInstalledPacks(['dedupe-kr-extended'])

    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(themeDetailFn).toHaveBeenCalled())

    await waitFor(() => {
      expect(result.current.allThemes.map(t => t.value)).toContain('custom-dedupe-kr-extended')
    })
  })
})
