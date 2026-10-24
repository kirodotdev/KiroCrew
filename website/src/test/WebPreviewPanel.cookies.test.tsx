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
      importBrowserCookies: (content: string, sessionKey?: string) =>
        importBrowserCookies(content, sessionKey),
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
const ABSENT = { present: false, summary: null }
const PRESENT = { present: true, summary: SUMMARY }

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
      '{"cookies":[{"name":"s","domain":"example.com"}]}', 'sess-1',
    ))
    const chip = await screen.findByTestId('web-preview-cookies-chip')
    expect(chip.textContent).toContain('34')
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-dialog')).toBeNull())
  })

  it('sends a Netscape export unchanged, keeping the trailing tab of an empty-valued last cookie', async () => {
    importBrowserCookies.mockResolvedValue({ ok: true, summary: SUMMARY, hot_load: { loaded: ['kc-1'], failed: {} } })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    const textarea = dialog.querySelector('textarea') as HTMLTextAreaElement
    // The last line's value column is empty, so the line ends in a tab.
    const exported = '.example.com\tTRUE\t/\tTRUE\t0\tsid\tabc\n.example.com\tTRUE\t/\tTRUE\t0\tempty\t'
    fireEvent.change(textarea, { target: { value: exported } })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    await waitFor(() => expect(importBrowserCookies).toHaveBeenCalledWith(exported, 'sess-1'))
  })

  it('disables draft controls during a pending import and re-enables them after incomplete delivery', async () => {
    const result = {
      ok: true,
      summary: SUMMARY,
      hot_load: { loaded: ['kc-1'], failed: { 'kc-2': 'timed out' } },
    }
    let resolveImport!: (value: typeof result) => void
    importBrowserCookies.mockReturnValue(new Promise<typeof result>((resolve) => {
      resolveImport = resolve
    }))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    const textarea = within(dialog).getByRole('textbox') as HTMLTextAreaElement
    const chooseFile = within(dialog).getByRole('button', { name: 'Choose a file' }) as HTMLButtonElement
    const fileInput = screen.getByTestId('web-preview-cookies-file') as HTMLInputElement
    const exported = '{"cookies":[{"name":"s","domain":"example.com"}]}'
    expect(textarea.disabled).toBe(false)
    expect(chooseFile.disabled).toBe(false)
    expect(fileInput.disabled).toBe(false)
    fireEvent.change(textarea, { target: { value: exported } })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    await waitFor(() => {
      expect(importBrowserCookies).toHaveBeenCalledWith(exported, 'sess-1')
      expect(textarea.disabled).toBe(true)
      expect(chooseFile.disabled).toBe(true)
      expect(fileInput.disabled).toBe(true)
    })

    resolveImport(result)
    await screen.findByTestId('web-preview-cookies-live-error')
    await waitFor(() => {
      expect(textarea.disabled).toBe(false)
      expect(chooseFile.disabled).toBe(false)
      expect(fileInput.disabled).toBe(false)
    })
    expect(screen.getByTestId('web-preview-cookies-dialog')).toBeTruthy()
    expect(textarea.value).toBe(exported)
    expect(screen.getByRole('button', { name: 'Done' })).toBeTruthy()
    fireEvent.change(textarea, { target: { value: 'next export' } })
    expect(screen.queryByRole('button', { name: 'Done' })).toBeNull()
    expect(screen.getByTestId('web-preview-cookies-submit')).toBeTruthy()
    expect(importBrowserCookies).toHaveBeenCalledTimes(1)
  })

  it('keeps the dialog open and says so when some live sessions refused the import', async () => {
    importBrowserCookies.mockResolvedValue({
      ok: true,
      summary: SUMMARY,
      hot_load: { loaded: ['kc-1'], failed: { 'kc-2': 'timed out' } },
    })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    const textarea = dialog.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: '{"cookies":[{"name":"s","domain":"example.com"}]}' } })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    // The set was saved, but loading some live browsers failed: an alert.
    const notice = await screen.findByTestId('web-preview-cookies-live-error')
    expect(notice.getAttribute('role')).toBe('alert')
    // The set is saved, so the hand-off cannot lose anything: it is offered.
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeTruthy()
    expect(textarea.value).toBe('{"cookies":[{"name":"s","domain":"example.com"}]}')
    expect(notice.textContent).toContain('Imported. 1 open agent browser(s) did not take the cookies yet')
    expect(notice.textContent).toContain('when it next starts')
    // Saved all the same: the chip shows the new set, and the dialog stays open.
    expect((await screen.findByTestId('web-preview-cookies-chip')).textContent).toContain('34')
    expect(screen.getByTestId('web-preview-cookies-dialog')).toBeTruthy()
    expect(screen.queryByTestId('web-preview-cookies-error')).toBeNull()
  })

  it.each([{ loaded: [] }, { loaded: ['kc-1'] }])('keeps an unreachable import open with an alert and no new-session hint (%j)', async ({ loaded }) => {
    getBrowserView.mockResolvedValue({ status: 'running', url: 'http://127.0.0.1:45613/', port: 45613, reason: null })
    importBrowserCookies.mockResolvedValue({
      ok: true, summary: SUMMARY,
      hot_load: { deliverable: false, loaded, failed: {} },
    })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTitle('Live browser session')
    fireEvent.click(screen.getByRole('button', { name: 'Browser view' }))
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    const textarea = within(dialog).getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: '{"cookies":[{"name":"s","domain":"example.com"}]}' } })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    const notice = await screen.findByTestId('web-preview-cookies-live-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain("Saved, but the agent's browsers on this computer can't receive imported cookies, so no agent browser will be signed in. Clear cookies removes the saved set.")
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeTruthy()
    expect(screen.getByTestId('web-preview-cookies-dialog')).toBeTruthy()
    expect(textarea.value).not.toBe('')
    expect(screen.queryByTestId('web-preview-cookies-new-session-hint')).toBeNull()
    expect((await screen.findByTestId('web-preview-cookies-chip')).textContent).toContain('34')
  })

  it.each([
    { deliverable: false, loaded: [], failed: {} },
    { deliverable: true, loaded: ['kc-1'], failed: { 'kc-2': 'timed out' } },
    { deliverable: true, loaded: [], failed: { 'kc-2': 'timed out' } },
  ])('offers only Done after a saved incomplete import, and restores Import on edit (%j)', async (hot_load) => {
    importBrowserCookies.mockResolvedValue({ ok: true, summary: SUMMARY, hot_load })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    const textarea = within(dialog).getByRole('textbox')
    fireEvent.change(textarea, { target: { value: '{"cookies":[{"name":"s","domain":"example.com"}]}' } })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    const done = await screen.findByRole('button', { name: 'Done' })
    const footer = done.parentElement as HTMLElement
    expect(within(footer).getAllByRole('button')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
    expect(screen.queryByTestId('web-preview-cookies-submit')).toBeNull()
    expect(screen.getByTestId('web-preview-cookies-live-error').getAttribute('role')).toBe('alert')
    expect(importBrowserCookies).toHaveBeenCalledTimes(1)

    fireEvent.change(textarea, { target: { value: '{"cookies":[{"name":"edited","domain":"example.com"}]}' } })
    expect(screen.queryByRole('button', { name: 'Done' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
    expect(screen.getByTestId('web-preview-cookies-submit')).toBeTruthy()
    expect(importBrowserCookies).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    fireEvent.click(await screen.findByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-dialog')).toBeNull())
    expect(importBrowserCookies).toHaveBeenCalledTimes(2)

    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const reopened = await screen.findByTestId('web-preview-cookies-dialog')
    expect((within(reopened).getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
    expect(screen.queryByTestId('web-preview-cookies-live-error')).toBeNull()
    expect(screen.getByTestId('web-preview-cookies-submit')).toBeTruthy()
  })

  it('offers the hand-off on a saved-but-not-delivered import, never on a failed one', async () => {
    // A failed import: the paste was NOT saved, so no hand-off may unmount it.
    importBrowserCookies.mockRejectedValueOnce(new ApiError(400, 'That is not a cookie export'))
    importBrowserCookies.mockResolvedValueOnce({
      ok: true, summary: SUMMARY,
      hot_load: { deliverable: false, loaded: [], failed: {} },
    })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    const textarea = within(dialog).getByRole('textbox')
    fireEvent.change(textarea, { target: { value: 'garbage' } })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    const err = await screen.findByTestId('web-preview-cookies-error')
    expect(within(err).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
    expect(screen.queryByTestId('web-preview-cookies-live-error')).toBeNull()

    // The retry saves but cannot be delivered: the set is persisted and the
    // notice clears on any edit, so the hand-off is offered.
    fireEvent.change(textarea, { target: { value: '{"cookies":[{"name":"s","domain":"example.com"}]}' } })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    const live = await screen.findByTestId('web-preview-cookies-live-error')
    expect(screen.queryByTestId('web-preview-cookies-error')).toBeNull()
    expect(within(live).getByRole('button', { name: /Ask the agent/ })).toBeTruthy()

    // An edit clears the notice, and its hand-off with it.
    fireEvent.change(textarea, { target: { value: '{"cookies":[{"name":"edited","domain":"example.com"}]}' } })
    expect(screen.queryByTestId('web-preview-cookies-live-error')).toBeNull()
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).toBeNull()
  })

  it('says a failed status read beside the Import item, with a hand-off', async () => {
    getBrowserCookies.mockRejectedValue(new ApiError(500, 'state unreadable'))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    // The hook retries a bounded number of times, then settles as an error.
    await waitFor(() => expect(getBrowserCookies).toHaveBeenCalledTimes(3), { timeout: 8000 })
    openOverflow()
    const err = await screen.findByTestId('web-preview-cookies-status-error', {}, { timeout: 8000 })
    expect(err.getAttribute('role')).toBe('alert')
    expect(err.textContent).toContain('state unreadable')
    const handoff = screen.getByRole('menuitem', { name: /Ask the agent/ })
    expect(handoff.getAttribute('aria-describedby')).toBe(err.getAttribute('id'))
  }, 15000)

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

  it('names the sites and the cookie count before Import is pressed', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    expect(screen.queryByTestId('web-preview-cookies-preview')).toBeNull()
    const textarea = dialog.querySelector('textarea') as HTMLTextAreaElement
    const exported = JSON.stringify({
      cookies: ['a.example', 'b.example', '.c.example', 'd.example', 'e.example'].map((domain, i) => ({
        name: `n${i}`, value: 'secret-value', domain,
      })),
    })
    fireEvent.change(textarea, { target: { value: exported } })
    const preview = await screen.findByTestId('web-preview-cookies-preview')
    expect(preview.textContent).toBe('Import signs the agent in to a.example, b.example, c.example, +2 more (5 cookies).')
    expect(preview.textContent).not.toContain('secret-value')
    // Nothing is sent until the user presses Import.
    expect(importBrowserCookies).not.toHaveBeenCalled()
    // An export the reader cannot parse shows no preview; the server says why.
    fireEvent.change(textarea, { target: { value: 'garbage' } })
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-preview')).toBeNull())
  })

  it('opens with which sites, for how long and how to undo, then a How to export list', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByLabelText(/preview url/i)
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    // The first thing in the body is the plain sentence, not a list of formats.
    const intro = within(dialog).getByTestId('web-preview-cookies-intro')
    expect(dialog.firstElementChild).toBe(intro)
    expect(intro.textContent).toContain('sites in your export')
    expect(intro.textContent).toContain('until those cookies expire')
    expect(intro.textContent).toContain('Clear cookies')
    // What the agent can do once signed in, when, and who can ask it to.
    expect(intro.textContent).toContain('open, read and act on the pages as you')
    expect(intro.textContent).toContain('anyone you let chat with this agent')
    expect(intro.textContent).toContain('Slack or Discord members you allowed')
    const list = within(dialog).getByTestId('web-preview-cookies-how-to-export')
    expect(within(dialog).getByText('How to export')).toBeTruthy()
    expect(list.querySelectorAll('li')).toHaveLength(3)
    expect(intro.textContent).not.toContain('cookies.txt')
  })

  it('keeps every keystroke in the dialog away from the page shortcuts', async () => {
    // The page's global shortcuts listen on `document` in the bubble phase, and
    // some fire with an input focused (Ctrl+digit jumps to a session). A chord
    // that reached them would navigate away and unmount the dialog with the
    // unsaved paste in it.
    const pageShortcut = vi.fn()
    document.addEventListener('keydown', pageShortcut)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      await screen.findByLabelText(/preview url/i)
      openOverflow()
      fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
      const dialog = await screen.findByTestId('web-preview-cookies-dialog')
      const textarea = dialog.querySelector('textarea') as HTMLTextAreaElement
      fireEvent.change(textarea, { target: { value: 'draft export' } })
      pageShortcut.mockClear()
      fireEvent.keyDown(textarea, { key: '1', ctrlKey: true })
      fireEvent.keyDown(textarea, { key: ',', metaKey: true })
      expect(pageShortcut).not.toHaveBeenCalled()
      expect((screen.getByTestId('web-preview-cookies-dialog').querySelector('textarea') as HTMLTextAreaElement).value).toBe('draft export')
    } finally {
      document.removeEventListener('keydown', pageShortcut)
    }
  })

  it('renders a file that cannot be read through ErrorNotice, not as a hint', async () => {
    class FailingReader {
      result: string | null = null
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      readAsText() { queueMicrotask(() => this.onerror?.()) }
    }
    vi.stubGlobal('FileReader', FailingReader)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      await screen.findByLabelText(/preview url/i)
      openOverflow()
      fireEvent.click(await screen.findByTestId('web-preview-import-cookies'))
      await screen.findByTestId('web-preview-cookies-dialog')
      const input = screen.getByTestId('web-preview-cookies-file') as HTMLInputElement
      const file = new File(['x'], 'export.json', { type: 'application/json' })
      fireEvent.change(input, { target: { files: [file] } })
      const err = await screen.findByTestId('web-preview-cookies-error')
      expect(err.getAttribute('role')).toBe('alert')
      expect(err.textContent).toContain("Couldn't read that file")
      expect(screen.queryByTestId('web-preview-cookies-hint')).toBeNull()
      expect(importBrowserCookies).not.toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
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
    clearBrowserCookies.mockResolvedValue({ ok: true, present: false, live: { cleared: ['kc-abc'], failed: {} } })
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

  it('reports a partial clear through an alert on the disabled row with a sibling hand-off', async () => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    clearBrowserCookies.mockResolvedValue({
      ok: true,
      present: false,
      live: { cleared: [], failed: { 'kc-abc': 'socket closed', 'kc-def': 'timeout' } },
    })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear')) // arm
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear')) // confirm
    await waitFor(() => expect(clearBrowserCookies).toHaveBeenCalledWith('sess-1'))
    // The on-disk set is gone: the chip leaves…
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-chip')).toBeNull())
    // …but the failed live clears remain on the (now inert) row.
    const notice = await screen.findByTestId('web-preview-cookies-clear-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('Cleared. 2 open agent browser(s) stay signed in until they close')
    const row = screen.getByTestId('web-preview-cookies-clear')
    expect(row.contains(notice)).toBe(true)
    expect(notice.querySelector('button')).toBeNull()
    expect(row.getAttribute('aria-disabled')).toBe('true')
    const handoff = screen.getByRole('menuitem', { name: /Ask the agent/ })
    expect(notice.getAttribute('id')).toBeTruthy()
    expect(handoff.getAttribute('aria-describedby')).toBe(notice.getAttribute('id'))
    expect(row.contains(handoff)).toBe(false)
    expect(handoff.getAttribute('aria-disabled')).not.toBe('true')
  })

  it('keeps a rejected pending clear notice after Escape and reopening the menu', async () => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    let rejectClear!: (error: ApiError) => void
    clearBrowserCookies.mockReturnValue(new Promise((_, reject) => { rejectClear = reject }))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    await waitFor(() => {
      expect(clearBrowserCookies).toHaveBeenCalledWith('sess-1')
      expect(screen.getByTestId('web-preview-cookies-clear').getAttribute('aria-disabled')).toBe('true')
    })
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    rejectClear(new ApiError(500, 'disk on fire'))
    openOverflow()
    const notice = await screen.findByTestId('web-preview-cookies-clear-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('disk on fire')
    expect(screen.getByTestId('web-preview-cookies-chip')).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: /Ask the agent/ }).getAttribute('aria-describedby'))
      .toBe(notice.getAttribute('id'))
  })

  it('keeps a partial pending clear notice after Escape and reopening the menu', async () => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    const partial = { ok: true, present: false, live: { cleared: [], failed: { 'kc-abc': 'timeout' } } }
    let resolveClear!: (value: typeof partial) => void
    clearBrowserCookies.mockReturnValue(new Promise<typeof partial>((resolve) => { resolveClear = resolve }))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    await waitFor(() => {
      expect(clearBrowserCookies).toHaveBeenCalledWith('sess-1')
      expect(screen.getByTestId('web-preview-cookies-clear').getAttribute('aria-disabled')).toBe('true')
    })
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    resolveClear(partial)
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-chip')).toBeNull())
    openOverflow()
    const notice = await screen.findByTestId('web-preview-cookies-clear-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('Cleared. 1 open agent browser(s) stay signed in until they close')
    expect(screen.getByTestId('web-preview-cookies-clear').getAttribute('aria-disabled')).toBe('true')
    expect(screen.getByRole('menuitem', { name: /Ask the agent/ }).getAttribute('aria-describedby'))
      .toBe(notice.getAttribute('id'))
  })

  it('a successful import removes a partial clear notice', async () => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    clearBrowserCookies.mockResolvedValue({
      ok: true, present: false, live: { cleared: [], failed: { 'kc-abc': 'timeout' } },
    })
    importBrowserCookies.mockResolvedValue({
      ok: true, summary: SUMMARY, hot_load: { loaded: ['kc-abc'], failed: {} },
    })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    await screen.findByTestId('web-preview-cookies-clear-error')
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-chip')).toBeNull())
    fireEvent.click(screen.getByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    fireEvent.change(within(dialog).getByRole('textbox'), {
      target: { value: '{"cookies":[{"name":"s","domain":"example.com"}]}' },
    })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-dialog')).toBeNull())
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    const row = await screen.findByTestId('web-preview-cookies-clear')
    expect(row.getAttribute('aria-disabled')).not.toBe('true')
    expect(screen.queryByTestId('web-preview-cookies-clear-error')).toBeNull()
    expect(screen.queryByRole('menuitem', { name: /Ask the agent/ })).toBeNull()
  })

  it('removes a partial clear notice when a refetch finds externally imported cookies', async () => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    clearBrowserCookies.mockResolvedValue({
      ok: true, present: false, live: { cleared: [], failed: { 'kc-abc': 'timeout' } },
    })
    const { queryClient } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    await screen.findByTestId('web-preview-cookies-clear-error')
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-chip')).toBeNull())
    await queryClient.refetchQueries({ queryKey: ['browserCookies', 'sess-1'] })
    await screen.findByTestId('web-preview-cookies-chip')
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-clear-error')).toBeNull())
    expect(screen.getByTestId('web-preview-cookies-clear').getAttribute('aria-disabled')).not.toBe('true')
    expect(screen.queryByRole('menuitem', { name: /Ask the agent/ })).toBeNull()
  })

  it.each(['rejected', 'partial'] as const)('does not show a %s clear notice in another slot without remounting', async (outcome) => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    if (outcome === 'rejected') clearBrowserCookies.mockRejectedValue(new ApiError(500, 'disk on fire'))
    else clearBrowserCookies.mockResolvedValue({
      ok: true, present: false, live: { cleared: [], failed: { 'kc-abc': 'timeout' } },
    })
    const { rerender } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    await screen.findByTestId('web-preview-cookies-clear-error')
    rerender(<WebPreviewPanel sessionKey="sess-2" />)
    await waitFor(() => expect(getBrowserCookies).toHaveBeenCalledWith('sess-2'))
    await screen.findByTestId('web-preview-cookies-chip')
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-clear-error')).toBeNull())
    expect(screen.getByTestId('web-preview-cookies-clear').getAttribute('aria-disabled')).not.toBe('true')
  })

  it('keeps the Clear row disabled after importing while DELETE is still pending', async () => {
    getBrowserCookies.mockResolvedValue(PRESENT)
    const cleared = { ok: true, present: false, live: { cleared: [], failed: {} } }
    let resolveClear!: (value: typeof cleared) => void
    clearBrowserCookies.mockReturnValue(new Promise<typeof cleared>((resolve) => { resolveClear = resolve }))
    importBrowserCookies.mockResolvedValue({
      ok: true, summary: SUMMARY, hot_load: { loaded: ['kc-abc'], failed: {} },
    })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTestId('web-preview-cookies-chip')
    openOverflow()
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    fireEvent.click(await screen.findByTestId('web-preview-cookies-clear'))
    await waitFor(() => {
      expect(clearBrowserCookies).toHaveBeenCalledTimes(1)
      expect(screen.getByTestId('web-preview-cookies-clear').getAttribute('aria-disabled')).toBe('true')
    })
    fireEvent.click(screen.getByTestId('web-preview-import-cookies'))
    const dialog = await screen.findByTestId('web-preview-cookies-dialog')
    fireEvent.change(within(dialog).getByRole('textbox'), {
      target: { value: '{"cookies":[{"name":"s","domain":"example.com"}]}' },
    })
    fireEvent.click(screen.getByTestId('web-preview-cookies-submit'))
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-dialog')).toBeNull())
    openOverflow()
    const row = await screen.findByTestId('web-preview-cookies-clear')
    expect(row.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(row)
    fireEvent.click(row)
    expect(clearBrowserCookies).toHaveBeenCalledTimes(1)
    resolveClear(cleared)
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-clear')).toBeNull())
    await waitFor(() => expect(screen.queryByTestId('web-preview-cookies-chip')).toBeNull())
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
