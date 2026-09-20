import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import WebPreviewPanel from '../components/WebPreviewPanel'
import { ApiError } from '../api/apiError'

// Force the crop button's capability on (unrelated), matching the sibling suite.
vi.mock('../hooks/useScreenSnip', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useScreenSnip')>()
  return { ...actual, isScreenSnipSupported: () => true }
})

// Stub the api seam: the browser-view status (kept `stopped` so the preview
// toolbar — where the Import cookies menu item and chip live — is the surface
// under test) and the three cookie methods. The rest of the client, including
// ApiError, stays real so the hook's status branching is exercised end to end.
const getBrowserView = vi.fn()
const startBrowserView = vi.fn()
const openInBrowser = vi.fn()
const getBrowserCookies = vi.fn()
const importBrowserCookies = vi.fn()
const clearBrowserCookies = vi.fn()
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getBrowserView: () => getBrowserView(),
      startBrowserView: () => startBrowserView(),
      openInBrowser: (url: string, sessionKey: string) => openInBrowser(url, sessionKey),
      getBrowserCookies: (sessionKey?: string) => getBrowserCookies(sessionKey),
      importBrowserCookies: (content: string, filename?: string, sessionKey?: string) =>
        importBrowserCookies(content, filename, sessionKey),
      clearBrowserCookies: (sessionKey?: string) => clearBrowserCookies(sessionKey),
    },
  }
})

const STOPPED = { status: 'stopped', url: null, port: null, reason: null }
const SUMMARY = {
  cookie_count: 34,
  domains: ['example.com', 'api.example.com'],
  earliest_expiry: 1_800_000_000,
  imported_at: 1_700_000_000,
}
const ABSENT = { present: false, summary: null, config_path: '/p/browser-storage-state.json' }
const PRESENT = { present: true, summary: SUMMARY, config_path: '/p/browser-storage-state.json' }

/** Open the toolbar overflow ("More actions") menu the cookie item lives in. */
function openOverflow() {
  fireEvent.pointerDown(
    screen.getByRole('button', { name: 'More actions' }),
    { pointerId: 1, button: 0, ctrlKey: false, isPrimary: true },
  )
}

beforeEach(() => {
  getBrowserView.mockReset().mockResolvedValue(STOPPED)
  startBrowserView.mockReset().mockResolvedValue(STOPPED)
  openInBrowser.mockReset()
  getBrowserCookies.mockReset().mockResolvedValue(ABSENT)
  importBrowserCookies.mockReset()
  clearBrowserCookies.mockReset()
})

describe('WebPreviewPanel — Import cookies', () => {
  it('offers Import cookies in the overflow menu', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    expect(await screen.findByTestId('web-preview-import-cookies')).toBeTruthy()
  })

  it('opens the import dialog from the menu item', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    expect(await screen.findByTestId('web-preview-cookies-dialog')).toBeTruthy()
    expect(screen.getByTestId('web-preview-cookies-submit')).toBeTruthy()
  })

  it('imports pasted cookies, calls the API and shows the status chip', async () => {
    importBrowserCookies.mockResolvedValue({ ok: true, summary: SUMMARY, hot_load: { loaded: ['kc-1'], failed: {} } })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    const textarea = dialog.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: '{"cookies":[{"name":"s","domain":"example.com"}]}' } })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    // The active slot's key rides the import, so the gateway's restricted-session
    // guard sees the real slot rather than the shared `dashboard:ui` placeholder.
    await waitFor(() => expect(importBrowserCookies).toHaveBeenCalledWith(
      '{"cookies":[{"name":"s","domain":"example.com"}]}', undefined, 'sess-1',
    ))
    const chip = await screen.findByTestId('web-preview-cookies-chip')
    expect(chip.textContent).toContain('34')
    expect(screen.queryByTestId('web-preview-cookies-dialog')).toBeNull()
  })

  it('reads the cookie status on behalf of the active slot', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await waitFor(() => expect(getBrowserCookies).toHaveBeenCalledWith('sess-1'))
  })

  it('shows a client-side hint (not an error surface) for an empty paste', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    await screen.findByTestId('web-preview-cookies-dialog')
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    const hint = await screen.findByTestId('web-preview-cookies-hint')
    expect(hint.textContent).toContain('Paste an export or choose a file first')
    // Nothing failed on the server: no request, and no ErrorNotice.
    expect(importBrowserCookies).not.toHaveBeenCalled()
    expect(screen.queryByTestId('web-preview-cookies-error')).toBeNull()
    expect(hint.getAttribute('role')).not.toBe('alert')
  })

  it('renders a failed import through ErrorNotice (no hand-off) and keeps the dialog open', async () => {
    importBrowserCookies.mockRejectedValue(new ApiError(400, 'That is not a cookie export'))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    const textarea = dialog.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: 'garbage' } })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    const err = await screen.findByTestId('web-preview-cookies-error')
    // The shared ErrorNotice surface: role="alert", the server's own message.
    expect(err.getAttribute('role')).toBe('alert')
    expect(err.textContent).toContain('That is not a cookie export')
    // No hand-off next to the unsaved paste: the textarea keeps its draft.
    expect(err.querySelector('button')).toBeNull()
    expect((screen.getByTestId('web-preview-cookies-dialog').querySelector('textarea') as HTMLTextAreaElement).value).toBe('garbage')
  })

  it('keeps the status chip read-only (no Clear control on it)', async () => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const chip = await screen.findByTestId('web-preview-cookies-chip')
    expect(chip.querySelector('button')).toBeNull()
    expect(chip.getAttribute('title')).toContain('example.com')
    expect(screen.queryByTestId('web-preview-cookies-clear')).toBeNull()
  })

  it('clears an imported set from the overflow menu on the two-step Clear item', async () => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    clearBrowserCookies.mockResolvedValue({ ok: true, present: false })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    const item = await screen.findByTestId('web-preview-cookies-clear')
    expect(item.textContent).toContain('Clear cookies')
    fireEvent.click(item) // arm — the menu stays open and the label flips
    expect(clearBrowserCookies).not.toHaveBeenCalled()
    const armed = await screen.findByTestId('web-preview-cookies-clear')
    expect(armed.textContent).toContain('Click again to clear')
    fireEvent.click(armed) // confirm
    await waitFor(() => expect(clearBrowserCookies).toHaveBeenCalledWith('sess-1'))
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-chip')).toBeNull())
    // Nothing to clear any more, so the item leaves the menu with the chip.
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-clear')).toBeNull())
  })

  it('does not offer Clear when nothing is imported', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    await screen.findByTestId('web-preview-import-cookies')
    expect(screen.queryByTestId('web-preview-cookies-clear')).toBeNull()
  })

  it('renders a failed Clear through ErrorNotice inside the menu with a sibling hand-off item', async () => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    clearBrowserCookies.mockRejectedValue(new ApiError(500, 'disk on fire'))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear')) // arm
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear')) // confirm
    const err = await screen.findByTestId('web-preview-cookies-clear-error')
    expect(err.getAttribute('role')).toBe('alert')
    expect(err.textContent).toContain('disk on fire')
    // The hand-off is a real menu focus stop, described by the passive alert.
    const handoff = screen.getByRole('menuitem', { name: /Ask the agent/ })
    expect(handoff.getAttribute('aria-describedby')).toBe(err.getAttribute('id'))
    // The set is still there — the chip stays.
    expect(screen.getByTestId('web-preview-cookies-chip')).toBeTruthy()
  })

  it('keeps the running Live-view header at its two actions (open + toggle) with a read-only chip', async () => {
    getBrowserView.mockResolvedValue({ status: 'running', url: 'http://127.0.0.1:45613/', port: 45613, reason: null })
    getBrowserCookies.mockResolvedValue(PRESENT)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const frame = await screen.findByTitle('Live browser session')
    const header = frame.parentElement!.parentElement!.firstElementChild as HTMLElement
    // The hidden preview toolbar keeps its own chip under the overlay; scope to
    // the header.
    const chip = await within(header).findByTestId('web-preview-cookies-chip')
    expect(chip.querySelector('button')).toBeNull()
    // max-two-buttons-per-row: the header carries open-in-browser + the toggle,
    // and nothing the cookie feature added counts (the chip is a span).
    const actions = Array.from(header.children).filter(
      (el) => el.tagName === 'BUTTON' || el.tagName === 'A' || el.getAttribute('role') === 'button',
    )
    expect(actions).toHaveLength(2)
    expect(screen.queryByTestId('web-preview-import-cookies-btn')).toBeNull()
  })

  it('hides the cookie control for a non-owner (403)', async () => {
    getBrowserCookies.mockRejectedValue(new ApiError(403, 'not owner'))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    // The overflow menu opens (Browser view item is there) but the cookie item is not.
    await screen.findByRole('menuitem', { name: /Browser view/ })
    expect(screen.queryByTestId('web-preview-import-cookies')).toBeNull()
    expect(screen.queryByTestId('web-preview-cookies-chip')).toBeNull()
  })
})
