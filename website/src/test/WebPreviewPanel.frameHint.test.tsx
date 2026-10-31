import type React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import WebPreviewPanel from '../components/WebPreviewPanel'

/**
 * The preview frame says why it may be blank (#2134).
 *
 * A target that answers requests but refuses to be FRAMED (X-Frame-Options,
 * CSP frame-ancestors) reaches the panel's last branch and renders as a bare
 * iframe, so without this affordance the reader faces an unexplained white
 * rectangle: the liveness probe passes (the server is up), the target is not
 * this gateway, and it is not an http-in-https refusal, so none of the four
 * explanatory cards fire.
 *
 * The hint is UNCONDITIONAL within the framing state because no client-side
 * signal separates a framed page from a refused one. A cross-origin frame fires
 * `load` either way and fires it SOONER for a refusal (milliseconds, against
 * seconds for a cold dev server), never fires `error`, exposes no readable
 * contentDocument even when healthy, and reports a zero-status resource timing
 * entry. `useSilentLoadWatch` cannot cover it either: its verdict is "load never
 * arrived", and here load always arrives. So these tests pin the two halves of
 * an unconditional affordance -- it is present for every framed target, and
 * absent wherever the body already explains itself.
 */

/** The Preview tab's allowlist as the tests' gateway answers it: every loopback
 *  host spelling on the ports the cases below type, so a case about framing is
 *  not refused by the gate (which has its own cases). */
const PORTS = [80, 443, 1234, 3000, 3001, 4000, 4321, 5000, 5173, 5174, 8000, 8080, 8081, 9000]
const ORIGINS = PORTS.flatMap(p => [`localhost:${p}`, `127.0.0.1:${p}`, `[::1]:${p}`])
const kirocrewConfig = vi.fn()
const patchConfig = vi.fn()
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      kirocrewConfig: () => kirocrewConfig(),
      patchConfig: (path: string, value: unknown) => patchConfig(path, value),
    },
  }
})

/** Render the panel and let its origins read settle: the allowlist gate shows
 *  neither a frame nor a refusal until the list has answered. */
async function renderPanel(ui: React.ReactElement) {
  const r = renderWithProviders(ui)
  await waitFor(() => expect(screen.getByTestId('web-preview-panel').getAttribute('data-origins-loaded')).toBe('true'))
  return r
}


const HINT = 'web-preview-frame-hint'

/** The address as the person picked it: links outside the dashboard keep it. */
const PICKED_AS_TYPED = 'http://localhost:3000/'

beforeEach(() => {
  localStorage.clear()
  kirocrewConfig.mockReset().mockResolvedValue({ dashboard: { browser_local_origins: ORIGINS } })
  patchConfig.mockReset().mockResolvedValue({ ok: true })
  // A reachable server by default, so the liveness probe does not take the
  // preview into its unreachable state while a test is about something else.
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(undefined))
})
afterEach(() => { vi.unstubAllGlobals() })

const submit = (raw: string) => {
  const input = screen.getByLabelText('Preview URL')
  fireEvent.change(input, { target: { value: raw } })
  fireEvent.submit(input.closest('form') as HTMLFormElement)
}

describe('WebPreviewPanel - a framed preview says why it may be blank', () => {
  it('shows the hint for an ordinary framed dev server', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    expect(screen.getByTitle('Web preview')).toBeInTheDocument()
    expect(screen.getByTestId(HINT)).toHaveTextContent('allow embedding and appear blank')
  })

  it('names its own category in words, and describes a class rather than this page', async () => {
    // Two separate burdens, both carried by the text. It must not read as a
    // verdict on the frame in front of the reader, which is what the class
    // phrasing buys, AND it must say it is a standing note rather than a status,
    // which the "Note:" label buys. A glyph carries the second only for a reader
    // who already knows the convention, while an unlabelled line of muted text
    // above a frame reads as a warning that has not cleared. "framed" stays out
    // too: the sibling cards in this panel say "embed".
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    const text = screen.getByTestId(HINT).textContent ?? ''
    expect(text).toMatch(/^Note: /)
    expect(text).toMatch(/Some pages /)
    expect(text).not.toMatch(/\bthis (preview|page)\b/i)
    expect(text).not.toMatch(/fram/i)
  })

  it('carries the open-in-browser escape hatch the reader needs', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    const link = screen.getByTestId(HINT).querySelector('a') as HTMLAnchorElement
    expect(link).not.toBeNull()
    expect(link.target).toBe('_blank')
    // The PRISTINE url, not the iframe's cache-busted src: the reader is being
    // handed the page's real address to open outside the dashboard.
    expect(link.getAttribute('href')).toBe(PICKED_AS_TYPED)
    expect(link.getAttribute('href')).not.toContain('_kcreload')
  })

  it('keeps pointing at the pristine url after a Reload varies the frame src', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    fireEvent.click(screen.getByLabelText('Reload preview'))
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.src).toContain('_kcreload')
    const link = screen.getByTestId(HINT).querySelector('a') as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe(PICKED_AS_TYPED)
  })

  it('shows the hint for a device-sized frame too', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    // Radix opens on pointerDown, not click.
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Preview size' }),
      { pointerId: 1, button: 0, ctrlKey: false, isPrimary: true },
    )
    fireEvent.click(screen.getByText('iPhone SE'))
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.style.width).toBe('375px')
    // The fixed-size branch is a different JSX subtree but the same framing
    // state, so a reader who shrank the viewport is not told less.
    expect(screen.getByTestId(HINT)).toBeInTheDocument()
  })
})

describe('WebPreviewPanel - the hint stays out of the way of a real explanation', () => {
  it('says nothing on the empty state, where there is no frame to be blank', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    expect(screen.queryByTestId(HINT)).toBeNull()
  })

  it('says nothing over the dashboard-origin card', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    submit(`${window.location.host}/api/hooks/agent`)
    expect(screen.getByText("Can't preview this dashboard here")).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    expect(screen.queryByTestId(HINT)).toBeNull()
  })

  it('says nothing once the dev server stops responding, and comes back with the frame', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('refused'))
    vi.stubGlobal('fetch', fetchMock)
    try {
      await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
      vi.useFakeTimers()
      fireEvent.click(screen.getByText('localhost:3000'))
      expect(screen.getByTestId(HINT)).toBeInTheDocument()
      // Two consecutive failed probes retire the frame for the stopped state.
      await act(async () => { await vi.advanceTimersByTimeAsync(11000) })
      expect(screen.getByText('Preview server not reachable')).toBeInTheDocument()
      expect(screen.queryByTestId(HINT)).toBeNull()
      // A later success restores the frame, and the hint with it.
      fetchMock.mockResolvedValue(undefined)
      await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
      expect(screen.getByTitle('Web preview')).toBeInTheDocument()
      expect(screen.getByTestId(HINT)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })

  it('says nothing while the outside-address card has replaced the frame', async () => {
    // The body swaps the frame for the refusal card, so a hint derived from
    // url-plus-reachability alone would sit above a card about a different
    // address, describing a frame that is not on screen.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    expect(screen.getByTestId(HINT)).toBeInTheDocument()

    submit('google.com')
    expect(screen.getByTestId('web-preview-outside-refused')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    expect(screen.queryByTestId(HINT)).toBeNull()
  })

  it('never disagrees with whether a frame is on screen', async () => {
    // The property behind every case above, asserted as one rule rather than
    // left implicit in a list of examples: the hint is shown exactly when the
    // body renders the iframe.
    const framedAndHinted = () => [
      screen.queryByTitle('Web preview') !== null,
      screen.queryByTestId(HINT) !== null,
    ]
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const [emptyFrame, emptyHint] = framedAndHinted()
    expect(emptyFrame).toBe(emptyHint)

    fireEvent.click(screen.getByText('localhost:3000'))
    const [liveFrame, liveHint] = framedAndHinted()
    expect(liveFrame).toBe(true)
    expect(liveHint).toBe(liveFrame)

    submit(`${window.location.host}/api/hooks/agent`)
    const [selfFrame, selfHint] = framedAndHinted()
    expect(selfFrame).toBe(false)
    expect(selfHint).toBe(selfFrame)
  })
})

describe('WebPreviewPanel - the hint reads as a standing note, not a live warning', () => {
  it('marks the row with an info glyph, not the warning glyph the cards use', async () => {
    // Wording cannot carry this distinction. Whether a line is a warning about
    // the reader's page or a note that is always there is a fact about the
    // line's PERSISTENCE, and no single view of the panel shows it, so the
    // glyph is what says which one this is. Asserting the class rather than the
    // presence of some svg, because AlertTriangle would also be "an icon" while
    // meaning the opposite.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    const row = screen.getByTestId(HINT)
    expect(row.querySelector('svg.lucide-info')).not.toBeNull()
    expect(row.querySelector('svg.lucide-alert-triangle')).toBeNull()
  })

  it('keeps the glyph out of the accessibility tree, since the sentence says it', async () => {
    // An ASSUMPTION pin, not a test of this change: lucide-react adds
    // aria-hidden="true" itself for a childless icon with no a11y prop
    // (Icon.js), so the explicit prop on the glyph is documentation and removing
    // it renders the same DOM. Pinned anyway, because the day that default
    // changes a decorative mark starts being announced next to a sentence that
    // already says it, and this goes red instead of a user noticing.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    const glyph = screen.getByTestId(HINT).querySelector('svg.lucide-info')
    expect(glyph?.getAttribute('aria-hidden')).toBe('true')
  })

  it('leaves every icon-only open-in-browser control labelled', async () => {
    // Load-bearing assumption, pinned so it cannot regress silently. The review
    // round that asked for a tooltip on the toolbar icon was answered by the
    // fact that it already has title and aria-label; a screenshot cannot show a
    // title, which is why the reading missed it. If someone drops that label,
    // the row's visible "Open in browser" really would be the only labelled way
    // out and the finding becomes correct.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    const links = Array.from(document.querySelectorAll('a')).filter(
      (a) => a.querySelector('svg.lucide-external-link') !== null,
    )
    // Split them, so this cannot pass on the row's own link alone: the row
    // labels itself with visible words, the toolbar control with attributes.
    const iconOnly = links.filter((a) => (a.textContent?.trim() ?? '') === '')
    const labelledByText = links.filter((a) => (a.textContent?.trim() ?? '') !== '')
    expect(iconOnly.length).toBeGreaterThan(0)
    expect(labelledByText.length).toBeGreaterThan(0)
    for (const a of iconOnly) {
      expect(a.getAttribute('aria-label')).toBe('Open in browser')
      expect(a.getAttribute('title')).toBe('Open in browser')
    }
    for (const a of labelledByText) {
      expect(a.textContent).toContain('Open in browser')
    }
  })
})
