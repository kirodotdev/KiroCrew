import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display → View: the billed credit-meter fallback toggle.
 *
 * Its own file because it needs the api client mocked (the shared
 * DisplayPanel.test.tsx deliberately runs against the real client): the switch
 * is server-persisted (dashboard.usage_text_scrape_enabled — the GATEWAY spends
 * the chat turn, so the value cannot live in browser storage) and written
 * through `api.patchConfig` behind the panel's per-path optimistic overlay,
 * the same shape as the Command completion toggle beside it.
 *
 * The load-bearing case is `reads as OFF when the key is absent`: this setting
 * gates a REAL billed LLM turn, so installing the control must not activate it.
 * A file that only exercised the flip would pass just as well if the row
 * defaulted to on.
 */

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => {
  /** Minimal stand-in with the same shape the panel reads (status + body). */
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  }
  return {
    api: {
      kirocrewConfig: kirocrewConfigMock,
      patchConfig: patchConfigMock,
      installTheme: vi.fn(() => Promise.resolve({ ok: true })),
    },
    ApiError,
  }
})

// Same provider doubles as the sibling DisplayPanel tests — the panel reads all
// of these on render and none is under test here.
const zoomCtx = {
  zoom: 100,
  zoomSupported: true,
  zoomIn: vi.fn(),
  zoomOut: vi.fn(),
  reset: vi.fn(),
  family: 'sans',
  setFontFamily: vi.fn(),
  cycleFamily: vi.fn(),
}
vi.mock('../hooks/ZoomProvider', () => ({
  useZoomCtx: () => zoomCtx,
}))

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    preference: 'dark',
    setTheme: vi.fn(),
    colorTheme: 'default',
    setColorTheme: vi.fn(),
    allThemes: [{ value: 'default', label: 'Default', custom: false }],
    theme: 'dark',
    themeVersion: 0,
    themeSwitching: false,
    addCustomTheme: vi.fn(),
    deleteCustomTheme: vi.fn(),
    loadCustomThemes: vi.fn(),
  }),
  ThemeProvider: ({ children }: { children: React.ReactNode }) => children,
  CUSTOM_THEMES_CHANGED_EVENT: 'custom-themes-changed',
}))

vi.mock('../hooks/useUIMode', () => ({
  useUIMode: () => ({
    uiMode: 'chat',
    setUIMode: vi.fn(),
    toggleUIMode: vi.fn(),
  }),
  UIModeProvider: ({ children }: { children: React.ReactNode }) => children,
}))

vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({
    paletteColors: ['#ff0000', '#00ff00', '#0000ff'],
    colorMode: 'tint' as const,
    paletteName: 'trailhead',
    intensity: 'clear',
    boost: {
      activePct: [60, 60, 60],
      idlePct: [30, 30, 30],
    },
  }),
}))

import { DisplayPanel } from '../pages/settings/DisplayPanel'

const KEY = 'dashboard.usage_text_scrape_enabled'
const LABEL = 'Spend a few credits to check your balance'

/** Seed the served config. `undefined` = a config that has never carried the key. */
function seed(value: unknown) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve(value === undefined ? { dashboard: {} } : { dashboard: { usage_text_scrape_enabled: value } }),
  )
}

const toggle = () => screen.findByRole('switch', { name: LABEL })

describe('DisplayPanel → billed credit-meter fallback', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    seed(undefined)
  })

  it('reads as OFF when the key is absent (the backend default)', async () => {
    renderWithProviders(<DisplayPanel />)
    expect(await toggle()).toHaveAttribute('aria-checked', 'false')
    // And nothing was written just by rendering the control.
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('reads as OFF for a literal false', async () => {
    seed(false)
    renderWithProviders(<DisplayPanel />)
    await waitFor(async () => expect(await toggle()).toHaveAttribute('aria-checked', 'false'))
  })

  it('reads as ON only for a literal true', async () => {
    seed(true)
    renderWithProviders(<DisplayPanel />)
    await waitFor(async () => expect(await toggle()).toHaveAttribute('aria-checked', 'true'))
  })

  it('does not read a hand-edited "true" string as on — the backend does not either', async () => {
    // config/sections.py `_safe_bool` returns the value only when it is a real
    // bool, else the default, so the gate reads this config as OFF. A switch
    // showing ON here would promise a fallback that never fires.
    seed('true')
    renderWithProviders(<DisplayPanel />)
    expect(await toggle()).toHaveAttribute('aria-checked', 'false')
  })

  it('PATCHes a boolean true on click, flipping optimistically', async () => {
    // A PATCH that never settles: the flip below can only come from the
    // per-path overlay, not from a refetch of the (stateless) config mock.
    patchConfigMock.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<DisplayPanel />)
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, true))
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
  })

  it('PATCHes false when switched off again', async () => {
    seed(true)
    patchConfigMock.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<DisplayPanel />)
    const sw = await toggle()
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, false))
  })

  it('surfaces a catalog message and rolls back when the save fails', async () => {
    patchConfigMock.mockImplementation(() => Promise.reject(new Error('boom')))
    renderWithProviders(<DisplayPanel />)
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    // The CATALOG copy renders — never the backend's sentence, which would ship
    // untranslated into every non-English locale.
    await waitFor(() =>
      expect(screen.getByText(/Could not save this setting/)).toBeInTheDocument(),
    )
    expect(screen.queryByText(/boom/)).not.toBeInTheDocument()
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
  })

  it('names the owner refusal instead of inviting a retry that cannot succeed', async () => {
    // Enabling is owner-only, refused 403 + `owner_only`. The generic line ends
    // "you can try again", which for a non-owner is a loop: the retry can never
    // succeed, so the row has to say why instead.
    patchConfigMock.mockImplementation(() =>
      Promise.reject(
        Object.assign(new Error('forbidden'), {
          status: 403,
          body: JSON.stringify({ error: 'owner authorization required', code: 'owner_only' }),
        }),
      ),
    )
    renderWithProviders(<DisplayPanel />)
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() =>
      expect(screen.getByText(/Only the dashboard owner can turn this on/)).toBeInTheDocument(),
    )
    expect(screen.queryByText(/you can try again/)).not.toBeInTheDocument()
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
  })

  it('keeps showing the saved value after a successful commit (no blink-back)', async () => {
    let stored: unknown = undefined
    patchConfigMock.mockImplementation(((_path: string, value: unknown) => {
      stored = value
      return Promise.resolve({})
    }) as never)
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({
        dashboard: stored === undefined ? {} : { usage_text_scrape_enabled: stored },
      }),
    )
    renderWithProviders(<DisplayPanel />)
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, true))
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    // A refetch round-trip later it still reads ON.
    await new Promise(r => setTimeout(r, 20))
    expect(sw).toHaveAttribute('aria-checked', 'true')
  })

  it('locks the switch while the PATCH is in flight, so two clicks cannot race', async () => {
    // Without the in-flight lock, rapid on-off clicks start two PATCHes over
    // separate connections; if they land out of order the server keeps `true`
    // after the user's final `false`, which for this key means billed refreshes
    // the user switched off. The lock makes the second click unrepresentable.
    patchConfigMock.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<DisplayPanel />)
    const sw = await toggle()
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, true))
    await waitFor(() => expect(sw).toHaveAttribute('aria-disabled', 'true'))
    fireEvent.click(sw)
    fireEvent.click(sw)
    // Still exactly the one write: no second, opposite choice is in flight.
    expect(patchConfigMock).toHaveBeenCalledTimes(1)
  })

  it('names the cost and how often it is charged, next to the switch', async () => {
    renderWithProviders(<DisplayPanel />)
    await toggle()
    // The billing consequence is the reason this setting is opt-in, and a cost
    // with no cadence is not a cost a user can weigh, so the row must carry
    // both rather than leaving either to be found on the next invoice.
    expect(screen.getByText(/about every 10 minutes/)).toBeInTheDocument()
    expect(screen.getByText(/Each check spends a small number of credits/)).toBeInTheDocument()
  })
})
