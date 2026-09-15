import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display: concurrent optimistic saves on the shared
 * ['kirocrewConfig'] key (#6890).
 *
 * The recency-tint stepper and the Default-shell field PATCH sibling paths of
 * one cached object. With a whole-object onMutate snapshot, a tint save that
 * starts BEFORE a shell save and then fails restores a pre-shell snapshot,
 * transiently reverting the in-flight shell save — the live race named in the
 * issue. Both writes now go through the per-path overlay, so a failure only
 * stops masking its own path.
 */

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn((_path: string, _value: unknown) => Promise.resolve({})),
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

// Same provider doubles as DisplayPanel.terminalShell.test.tsx — the panel
// reads all of these on render and none is under test here.
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

const TINT_PATH = 'dashboard.recent_tint_count'

/** The tint stepper's own field row — the panel has other steppers (zoom),
 *  so its buttons and value must be queried within this row. */
const tintField = () => {
  const el = document.querySelector('[data-setting-label="Highlight recent sessions"]')
  expect(el).not.toBeNull()
  return within(el as HTMLElement)
}
const SHELL_PATH = 'dashboard.terminal.shell'

function seed(shell: string, tint: number) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve({ dashboard: { terminal: { shell }, recent_tint_count: tint } }),
  )
}

/** Per-path deferred PATCHes: each config path gets its own held-open promise. */
function deferPatchesByPath() {
  const held: Record<string, { resolve: (v: unknown) => void; reject: (e: unknown) => void }> = {}
  patchConfigMock.mockImplementation(((path: string) => {
    return new Promise((resolve, reject) => { held[path] = { resolve, reject } })
  }) as never)
  return held
}

describe('DisplayPanel — concurrent tint/shell optimistic saves', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    seed('/bin/bash', 3)
  })

  it("a failed tint save does not revert an in-flight shell save's display", async () => {
    const held = deferPatchesByPath()
    renderWithProviders(<DisplayPanel />)
    const input = (await screen.findByLabelText('Default shell')) as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('/bin/bash'))

    // Tint save starts FIRST — the direction where a whole-object snapshot
    // captures the pre-shell state that a later rollback would restore.
    fireEvent.click(tintField().getByLabelText('Increase'))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(TINT_PATH, 4))

    // Shell save starts while the tint PATCH is still in flight.
    fireEvent.change(input, { target: { value: '/usr/bin/fish' } })
    fireEvent.blur(input)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(SHELL_PATH, '/usr/bin/fish'))

    // The tint save FAILS. Only the tint display may roll back.
    held[TINT_PATH].reject(new Error('boom'))
    await waitFor(() => expect(tintField().getByText('3')).toBeInTheDocument())

    // The shell save succeeds; the server now reports the new shell. Hold the
    // settle-time refetch open: this is exactly the window where the old
    // snapshot rollback left the cache on the pre-shell value and the input
    // (draft already cleared) blinked back to /bin/bash.
    seed('/usr/bin/fish', 3)
    let releaseRefetch!: (v: unknown) => void
    kirocrewConfigMock.mockImplementationOnce(
      () => new Promise(res => { releaseRefetch = res }) as never,
    )
    held[SHELL_PATH].resolve({})
    // Draft cleared after success…
    await waitFor(() => expect(input.value).toBe('/usr/bin/fish'))
    releaseRefetch({ dashboard: { terminal: { shell: '/usr/bin/fish' }, recent_tint_count: 3 } })
    // …and the value survives the refetch too.
    await waitFor(() => expect(input.value).toBe('/usr/bin/fish'))
  })

  it('the stepper shows the pending count immediately and stacks rapid clicks on it', async () => {
    const held = deferPatchesByPath()
    renderWithProviders(<DisplayPanel />)
    await screen.findByLabelText('Default shell')
    await waitFor(() => expect(tintField().getByText('3')).toBeInTheDocument())

    fireEvent.click(tintField().getByLabelText('Increase'))
    // Pre-settle: the PATCH is held open and the config mock still reports 3,
    // so the count reading 4 can only be coming from the overlay.
    await waitFor(() => expect(tintField().getByText('4')).toBeInTheDocument())

    // A second click steps from the SHOWN value, not the stale server one.
    fireEvent.click(tintField().getByLabelText('Increase'))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(TINT_PATH, 5))
    await waitFor(() => expect(tintField().getByText('5')).toBeInTheDocument())

    seed('/bin/bash', 5)
    held[TINT_PATH].resolve({})
    await waitFor(() => expect(tintField().getByText('5')).toBeInTheDocument())
  })
})
