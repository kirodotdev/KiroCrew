import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, within } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import WebPreviewPanel from '../components/WebPreviewPanel'

/**
 * A navigation inside the preview frame that the dashboard's own CSP refuses
 * is explained, not left as a blank frame (#22572).
 *
 * The case: a local app frames fine, then its "Continue with Google" button
 * redirects the frame to accounts.google.com. The dashboard's `frame-src`
 * refuses that navigation, the frame lands on the engine's error page, and the
 * button spins forever. Chromium reports the refusal as a
 * `securitypolicyviolation` event on the dashboard's document (target: the
 * document, `blockedURI`: the origin only), which is the signal these tests
 * replay. The event shape was checked against headless Chromium.
 *
 * Such a report cannot be tied to one frame, so the notice is announced above
 * the frame and never replaces it: a misattributed report must cost a line,
 * not the previewed app's state.
 */

const getBrowserView = vi.fn()
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: { ...actual.api, getBrowserView: () => getBrowserView() },
  }
})

const STOPPED = { status: 'stopped', url: null, port: null, reason: null }
const NOTICE = 'web-preview-frame-blocked'
const HINT = 'web-preview-frame-hint'

const iso = (h: string): string =>
  window.location.hostname === h ? (h === 'localhost' ? '127.0.0.1' : 'localhost') : h
const PICKED = `http://${iso('localhost')}:3000/`

/** Replay the event Chromium fires at the policy-owning document. */
function violate(
  { directive = 'frame-src', blocked = 'https://accounts.google.com', target }:
  { directive?: string; blocked?: string; target?: EventTarget } = {},
) {
  const e = new Event('securitypolicyviolation', { bubbles: true, composed: true })
  Object.defineProperty(e, 'effectiveDirective', { value: directive })
  Object.defineProperty(e, 'violatedDirective', { value: directive })
  Object.defineProperty(e, 'blockedURI', { value: blocked })
  act(() => { (target ?? document).dispatchEvent(e) })
}

beforeEach(() => {
  localStorage.clear()
  getBrowserView.mockReset().mockResolvedValue(STOPPED)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(undefined))
})
afterEach(() => { vi.unstubAllGlobals() })

/** Let updates a click scheduled (a menu closing, focus returning) land inside
 *  act before the test ends, instead of after it. */
async function settle() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

describe('WebPreviewPanel - a refused in-frame navigation is explained', () => {
  it('names the refusal, the refused origin and the way out above the frame', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    const frame = screen.getByTitle('Web preview')

    violate()

    const notice = screen.getByTestId(NOTICE)
    expect(notice).toHaveTextContent("This page can't open inside the preview")
    expect(notice).toHaveTextContent('Google or GitHub')
    expect(notice).toHaveTextContent('a new browser tab')
    // Any refused page gets this notice, so it does not tell the reader to sign in.
    expect(notice).not.toHaveTextContent(/sign in there/i)
    // The refused host is labelled, so it is not read as the app's own address.
    expect(notice).toHaveTextContent('Blocked: https://accounts.google.com')
    // A failed load renders through the shared error surface.
    expect(within(notice).getByRole('alert')).toBeInTheDocument()
    // The frame is announced over, never replaced: same element, still mounted.
    expect(screen.getByTitle('Web preview')).toBe(frame)
    // The standing note would say less than the notice beside it.
    expect(screen.queryByTestId(HINT)).toBeNull()
    // The way out opens the APP (pristine url), where sign-in runs top-level,
    // not the sign-in provider the frame was refused, and says so.
    const link = within(notice).getByRole('link', { name: 'Open in browser' })
    expect(link.getAttribute('href')).toBe(PICKED)
    expect(link.getAttribute('target')).toBe('_blank')
  })

  it('goes away on dismiss and leaves the frame as it was', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    const frame = screen.getByTitle('Web preview')
    violate()
    fireEvent.click(within(screen.getByTestId(NOTICE)).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId(NOTICE)).toBeNull()
    expect(screen.getByTitle('Web preview')).toBe(frame)
    expect(screen.getByTestId(HINT)).toBeInTheDocument()
    await settle()
  })

  it('survives switching side-panel tabs away and back', () => {
    // The panel stays mounted while its tab is inactive, and so does the
    // blank frame, so the notice explaining it must still be there on return.
    const { rerender } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(screen.getByText(':3000'))
    violate()
    rerender(<WebPreviewPanel sessionKey="sess-1" active={false} />)
    rerender(<WebPreviewPanel sessionKey="sess-1" active />)
    expect(screen.getByTestId(NOTICE)).toHaveTextContent('Blocked: https://accounts.google.com')
  })

  it('clears when the frame comes back after the server was unreachable', async () => {
    // The unreachable card unmounts the frame; a later good probe remounts it
    // on a fresh load, so the old refusal no longer describes it.
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('fetch', fetchMock)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      fireEvent.click(screen.getByText(':3000'))
      violate()
      expect(screen.getByTestId(NOTICE)).toBeInTheDocument()
      fetchMock.mockRejectedValue(new Error('refused'))
      await act(async () => { await vi.advanceTimersByTimeAsync(11000) })
      expect(screen.getByText('Preview server not reachable')).toBeInTheDocument()
      fetchMock.mockResolvedValue(undefined)
      await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
      expect(screen.getByTitle('Web preview')).toBeInTheDocument()
      expect(screen.queryByTestId(NOTICE)).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears on Reload', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    violate()
    fireEvent.click(screen.getByLabelText('Reload preview'))
    expect(screen.queryByTestId(NOTICE)).toBeNull()
    expect(screen.getByTitle('Web preview')).toBeInTheDocument()
  })

  it('clears when the reader loads another address', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    violate()
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'localhost:5173' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.queryByTestId(NOTICE)).toBeNull()
    expect(screen.getByTitle('Web preview')).toBeInTheDocument()
  })

  it('shows the notice over a device-sized frame too', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'More actions' }),
      { pointerId: 1, button: 0, ctrlKey: false, isPrimary: true },
    )
    fireEvent.click(screen.getByText('Preview size'))
    fireEvent.click(screen.getByText('iPhone SE'))
    violate({ target: screen.getByTitle('Web preview') })
    expect(screen.getByTestId(NOTICE)).toBeInTheDocument()
    expect(screen.getByTitle('Web preview')).toBeInTheDocument()
    await settle()
  })

  it('still explains a refusal whose target the engine did not name', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    violate({ blocked: '' })
    const notice = screen.getByTestId(NOTICE)
    expect(screen.queryByTestId('web-preview-frame-blocked-origin')).toBeNull()
    expect(within(notice).getByRole('link', { name: 'Open in browser' })).toBeInTheDocument()
  })
})

describe('WebPreviewPanel - other policy reports leave the frame alone', () => {
  it('ignores a refusal of the framed address itself', () => {
    // The frame's own first load being refused is not a navigation away from a
    // page that loaded; the panel's embed explanations own that case.
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    violate({ blocked: new URL(PICKED).origin })
    expect(screen.queryByTestId(NOTICE)).toBeNull()
  })

  it('keeps the frame mounted when a document-level report came from elsewhere', () => {
    // Another dashboard frame (an artifact preview, a chat widget) refused the
    // same way reports to the same document and cannot be told apart. The
    // most it may cost is the dismissable line: the preview, and any form
    // input in it, stays.
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    const frame = screen.getByTitle('Web preview')
    const other = document.createElement('iframe')
    document.body.appendChild(other)
    try {
      other.focus()
      violate()
      expect(screen.getByTitle('Web preview')).toBe(frame)
    } finally {
      other.remove()
    }
  })

  it('ignores a violation of another directive', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    violate({ directive: 'img-src', blocked: 'https://cdn.example.com' })
    expect(screen.queryByTestId(NOTICE)).toBeNull()
    expect(screen.getByTitle('Web preview')).toBeInTheDocument()
  })

  it('ignores a frame-src report aimed at some other element', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    const other = document.createElement('iframe')
    document.body.appendChild(other)
    try {
      violate({ target: other })
      expect(screen.queryByTestId(NOTICE)).toBeNull()
    } finally {
      other.remove()
    }
  })

  it('ignores a report while nothing is framed', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    violate()
    fireEvent.click(screen.getByText(':3000'))
    expect(screen.queryByTestId(NOTICE)).toBeNull()
    expect(screen.getByTitle('Web preview')).toBeInTheDocument()
  })
})
