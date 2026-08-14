/**
 * `gallery.tsx` — the avatar gallery's window entry.
 *
 * Not importable the ordinary way: the module MOUNTS ITSELF into `#root` at
 * import time, and only after awaiting the dashboard theme. So this harness is
 * the module's own bootstrap — register the mocks it reaches for, create (or
 * withhold) the host, then import it.
 *
 * The ordering is the contract worth pinning: the theme must be adopted BEFORE
 * the first paint, because the gallery is styled from Kiro Crew's variables and
 * painting ahead of them shows fallback colours and then snaps. And with no host
 * element the module must do nothing at all rather than throw during startup.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor } from '@testing-library/react'

/** Resolves the theme adopt on demand, so "before the first paint" is observable. */
let releaseTheme: (() => void) | null = null
const order: string[] = []

const adoptDashboardTheme = vi.fn(
  () =>
    new Promise<void>((resolve) => {
      releaseTheme = () => {
        order.push('theme')
        resolve()
      }
    }),
)
const watchThemeChanges = vi.fn(() => () => {})

vi.mock('../apps/crew-companion/dashboardTheme', () => ({
  adoptDashboardTheme: () => adoptDashboardTheme(),
  watchThemeChanges: () => watchThemeChanges(),
  applyThemeId: () => {},
  extractStylesheetHrefs: () => [],
}))

const initI18n = vi.fn()
vi.mock('../i18n', () => ({ initI18n: () => initI18n(), i18next: {} }))

// The panel owns the whole surface (including its own ✕ and Escape); stubbing it
// keeps every assertion here about the entry's own bootstrap.
vi.mock('../apps/crew-companion/GalleryPanel', () => ({
  GalleryPanel: () => {
    order.push('paint')
    return <div data-testid="gallery-panel" />
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
  vi.resetModules()
  order.length = 0
  releaseTheme = null
  // replaceChildren, not an innerHTML write: the frontend-security rule in
  // website/AUTOSDE.yaml is blocking and bans innerHTML writes under
  // src/**/*.tsx, tests included.
  document.body.replaceChildren()
})

afterEach(() => {
  document.body.replaceChildren()
})

describe('crew-companion/gallery entry', () => {
  it('initialises i18n, waits for the theme, then paints the panel', async () => {
    const host = document.createElement('div')
    host.id = 'root'
    document.body.appendChild(host)

    await import('../apps/crew-companion/gallery')

    // i18n is up front; nothing is painted while the theme is still pending.
    expect(initI18n).toHaveBeenCalledTimes(1)
    expect(adoptDashboardTheme).toHaveBeenCalledTimes(1)
    expect(order).toEqual([])

    releaseTheme?.()
    await waitFor(() => expect(document.querySelector('[data-testid="gallery-panel"]')).not.toBeNull())
    // The theme landed BEFORE the first paint, not alongside it. (StrictMode
    // renders the tree twice, so only the FIRST paint's position is meaningful.)
    expect(order[0]).toBe('theme')
    expect(order[1]).toBe('paint')
    expect(watchThemeChanges).toHaveBeenCalledTimes(1)
  })

  it('does nothing at all when the host element is absent', async () => {
    await import('../apps/crew-companion/gallery')
    expect(initI18n).not.toHaveBeenCalled()
    expect(adoptDashboardTheme).not.toHaveBeenCalled()
    expect(document.body.innerHTML).toBe('')
  })
})
