import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import WebPreviewPanel, { normalizeUrl, setSessionPreviewUrl, setSessionPreviewPending, isolatePreviewHost, isDashboardOrigin, withCacheBuster } from '../components/WebPreviewPanel'

// The crop button is gated on snip support (getDisplayMedia). Force it on so
// the button renders under happy-dom (which has no mediaDevices.getDisplayMedia).
vi.mock('../hooks/useScreenSnip', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useScreenSnip')>()
  return { ...actual, isScreenSnipSupported: () => true }
})

// Browser-view status/start and the address bar's launcher, stubbed at the api
// seam. Only these methods are replaced — the rest of the client (and ApiError,
// which the hook branches on) stays real, so no other call site in this panel
// changes behaviour.
const getBrowserView = vi.fn()
const startBrowserView = vi.fn()
const openInBrowser = vi.fn()
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getBrowserView: () => getBrowserView(),
      startBrowserView: () => startBrowserView(),
      openInBrowser: (url: string, sessionKey: string) => openInBrowser(url, sessionKey),
    },
  }
})

/** A view server that is up, on the loopback IPv4 address the CLI must bind. */
const RUNNING = { status: 'running', url: 'http://127.0.0.1:45613/', port: 45613, reason: null }
/** Installed, nothing serving yet — the state a start action is offered for. */
const STOPPED = { status: 'stopped', url: null, port: null, reason: null }
/** The launcher's answer for a page that opened in the gateway host's browser. */
const OPENED = (_url: string) => ({ ok: true, session: 'panel-1234abcd', error: null, view: RUNNING })

// Every test in this file renders the panel, which reads the view status. Default
// it to `stopped` so the tests that are about the dev-server preview neither hit
// an undefined query result nor get the browser view overlaid on them.
beforeEach(() => {
  getBrowserView.mockReset().mockResolvedValue(STOPPED)
  startBrowserView.mockReset().mockResolvedValue(RUNNING)
  openInBrowser.mockReset().mockImplementation(async (url: string) => OPENED(url))
})

// The panel isolates a loopback preview host equal to the dashboard host onto
// the other loopback alias. Compute what the code will produce so host
// assertions don't depend on the test env's window.location.hostname.
const iso = (h: string): string =>
  window.location.hostname === h ? (h === 'localhost' ? '127.0.0.1' : 'localhost') : h

/** The iframe's navigation TARGET, with the reload cache-buster stripped, so
 *  URL assertions stay about where the panel navigated rather than how many
 *  times it has reloaded. */
const targetOf = (frame: HTMLIFrameElement): string => {
  const u = new URL(frame.src)
  u.searchParams.delete('_kcreload')
  return u.toString()
}

describe('normalizeUrl', () => {
  it('adds an http scheme to a bare host:port', () => {
    expect(normalizeUrl('localhost:5173')).toBe('http://localhost:5173/')
    expect(normalizeUrl('127.0.0.1:8080')).toBe('http://127.0.0.1:8080/')
  })
  it('upgrades a bare public host to https (what a real site answers on)', () => {
    expect(normalizeUrl('google.com')).toBe('https://google.com/')
    expect(normalizeUrl('www.example.com/path?q=1')).toBe('https://www.example.com/path?q=1')
  })
  it('keeps http for the dev-server shapes: loopback, an IP literal, an explicit port', () => {
    expect(normalizeUrl('localhost')).toBe('http://localhost/')
    expect(normalizeUrl('myapp.localhost:5173')).toBe('http://myapp.localhost:5173/')
    expect(normalizeUrl('192.168.1.4')).toBe('http://192.168.1.4/')
    expect(normalizeUrl('192.168.1.4:3000')).toBe('http://192.168.1.4:3000/')
    expect(normalizeUrl('example.com:8443')).toBe('http://example.com:8443/')
  })
  it('keeps explicit http/https', () => {
    expect(normalizeUrl('https://example.com')).toBe('https://example.com/')
    expect(normalizeUrl('http://example.com')).toBe('http://example.com/')
  })
  it('rejects empty and non-http(s) schemes', () => {
    expect(normalizeUrl('   ')).toBeNull()
    expect(normalizeUrl('javascript:alert(1)')).toBeNull()
    expect(normalizeUrl('file:///etc/passwd')).toBeNull()
  })
})

describe('isolatePreviewHost', () => {
  it('swaps a loopback preview host that equals the dashboard host to the other alias', () => {
    expect(isolatePreviewHost('http://localhost:5173/', 'localhost')).toBe('http://127.0.0.1:5173/')
    expect(isolatePreviewHost('http://127.0.0.1:5173/', '127.0.0.1')).toBe('http://localhost:5173/')
  })
  it('isolates a same-host *.localhost dashboard (e.g. kirocrew.localhost) to 127.0.0.1', () => {
    expect(isolatePreviewHost('http://kirocrew.localhost:5173/', 'kirocrew.localhost'))
      .toBe('http://127.0.0.1:5173/')
  })
  it('leaves a preview host that already differs from the dashboard host', () => {
    expect(isolatePreviewHost('http://127.0.0.1:5173/', 'localhost')).toBe('http://127.0.0.1:5173/')
    expect(isolatePreviewHost('http://localhost:5173/', 'kirocrew.localhost')).toBe('http://localhost:5173/')
  })
  it('leaves non-loopback hosts untouched', () => {
    expect(isolatePreviewHost('https://example.com/', 'localhost')).toBe('https://example.com/')
  })
  it('is a no-op when the dashboard host is unknown', () => {
    expect(isolatePreviewHost('http://localhost:5173/', '')).toBe('http://localhost:5173/')
  })
  it('canonicalizes an IPv6 loopback ([::1]) preview host to 127.0.0.1 (CSP cannot admit [::1]:*)', () => {
    // The dashboard CSP structurally cannot admit `http://[::1]:*`, so the
    // liveness probe to [::1] is refused and a healthy server shows unreachable.
    expect(isolatePreviewHost('http://[::1]:8765/', 'localhost')).toBe('http://127.0.0.1:8765/')
    expect(isolatePreviewHost('http://[::1]:8765/app?x=1#h', 'localhost'))
      .toBe('http://127.0.0.1:8765/app?x=1#h')
  })
  it('canonicalizes [::1] even when the dashboard host is unknown (CSP gap is host-independent)', () => {
    expect(isolatePreviewHost('http://[::1]:8765/', '')).toBe('http://127.0.0.1:8765/')
  })
  it('canonicalizes [::1] then still cookie-isolates against a 127.0.0.1 dashboard', () => {
    // [::1] → 127.0.0.1, which now equals the dashboard host → isolate to localhost.
    expect(isolatePreviewHost('http://[::1]:8765/', '127.0.0.1')).toBe('http://localhost:8765/')
  })
})


describe('isDashboardOrigin', () => {
  it('matches the gateway across the two loopback aliases (same port)', () => {
    // The real case: the panel is on one alias, the target names the other, and
    // both are the same listening server — which refuses to be framed.
    expect(isDashboardOrigin('http://127.0.0.1:6776/api/hooks/agent', 'http://localhost:6776/')).toBe(true)
    expect(isDashboardOrigin('http://localhost:6776/', 'http://127.0.0.1:6776/chat')).toBe(true)
    expect(isDashboardOrigin('http://127.0.0.1:6776/', 'http://kirocrew.localhost:6776/')).toBe(true)
  })
  it('does not match a dev server on a different port', () => {
    expect(isDashboardOrigin('http://localhost:5173/', 'http://localhost:6776/')).toBe(false)
    expect(isDashboardOrigin('http://127.0.0.1:3000/', 'http://localhost:6776/')).toBe(false)
  })
  it('treats an absent port as the scheme default, so :80 and bare compare equal', () => {
    expect(isDashboardOrigin('http://localhost/', 'http://localhost:80/')).toBe(true)
    expect(isDashboardOrigin('http://localhost:80/', 'http://localhost/')).toBe(true)
    expect(isDashboardOrigin('http://localhost:8080/', 'http://localhost/')).toBe(false)
  })
  it('never matches a non-loopback target', () => {
    expect(isDashboardOrigin('https://example.com/', 'https://example.com/')).toBe(false)
  })
  it('never matches when the dashboard itself is remote', () => {
    // Over a tunnel or a LAN address, a loopback target is the USER's own
    // machine — an ordinary dev server — not this gateway.
    expect(isDashboardOrigin('http://localhost:443/', 'https://crew.example.com/')).toBe(false)
    expect(isDashboardOrigin('http://localhost:6776/', 'http://192.168.1.4:6776/')).toBe(false)
  })
  it('returns false for an unparseable url or an unknown dashboard origin', () => {
    expect(isDashboardOrigin('not a url', 'http://localhost:6776/')).toBe(false)
    expect(isDashboardOrigin('http://localhost:6776/', '')).toBe(false)
  })
})

describe('withCacheBuster', () => {
  it('appends the reload counter for any non-initial load', () => {
    expect(withCacheBuster('http://localhost:8080/', 1)).toBe('http://localhost:8080/?_kcreload=1')
    expect(withCacheBuster('http://localhost:8080/', 7)).toBe('http://localhost:8080/?_kcreload=7')
  })
  it('leaves the URL pristine for the initial load (key 0) and for an empty URL', () => {
    expect(withCacheBuster('http://localhost:8080/', 0)).toBe('http://localhost:8080/')
    expect(withCacheBuster('', 3)).toBe('')
  })
  it('preserves an existing query and fragment, inserting the param before the hash', () => {
    expect(withCacheBuster('http://localhost:5173/app?tab=logs#section-2', 2))
      .toBe('http://localhost:5173/app?tab=logs&_kcreload=2#section-2')
  })
  it('replaces its own param instead of stacking copies across reloads', () => {
    const once = withCacheBuster('http://localhost:5173/', 1)
    expect(withCacheBuster(once, 2)).toBe('http://localhost:5173/?_kcreload=2')
  })
  it('returns the input unchanged when it cannot be parsed', () => {
    expect(withCacheBuster('not a url', 1)).toBe('not a url')
  })
})

describe('WebPreviewPanel', () => {
  beforeEach(() => {
    localStorage.clear()
    // The liveness probe fetches the loaded URL; default it to "server up" so
    // the iframe stays mounted and no test hits the real network.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(undefined))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('shows the empty state with quick-pick ports before a URL is set', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    expect(screen.getByText(':5173')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    // Quick-pick buttons are type=button so a valid draft in the URL field
    // can't be overridden by a stray form submission.
    expect((screen.getByText(':5173').closest('button') as HTMLButtonElement).getAttribute('type')).toBe('button')
  })

  it('loads a typed URL into the iframe (normalizing scheme + isolating host) on submit', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'localhost:8080' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:8080/`)
  })

  it('enables back only after navigating to a second URL, and steps back', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByLabelText('Back')).toBeDisabled()
    fireEvent.click(screen.getByText(':3000'))
    expect(screen.getByLabelText('Back')).toBeDisabled()
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'localhost:5173' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    const back = screen.getByLabelText('Back')
    expect(back).not.toBeDisabled()
    fireEvent.click(back)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:3000/`)
    expect(screen.getByLabelText('Forward')).not.toBeDisabled()
  })

  it('loads a quick-pick port (isolated host)', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:3000/`)
  })

  it('persists the URL per session and restores it on mount', () => {
    localStorage.setItem('mc-webpreview-url:sess-1', 'http://localhost:4321/')
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:4321/`)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-2" />)
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('loads a URL fed externally via setSessionPreviewUrl (matching slot, isolated)', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    act(() => { setSessionPreviewUrl('sess-1', 'localhost:8080') })
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:8080/`)
  })

  it('does not live-load an external feed when open=false (offer only)', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewUrl('sess-1', 'localhost:8080', false) })
    // No dispatch → the already-mounted panel stays on the empty state.
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
  })

  it('ignores an external feed aimed at a different slot', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewUrl('sess-2', 'localhost:8080') })
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
  })

  it('shows a Load-preview card for a pending feed and navigates only on the explicit click', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewPending('sess-1', 'localhost:8080') })
    // Pending → a card is shown and the iframe is NOT loaded (no auto-GET).
    expect(screen.getByText('Preview ready')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    // Explicit click is what fires the load.
    fireEvent.click(screen.getByText('Load preview'))
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:8080/`)
  })

  it('rejects a NON-loopback chat-fed URL (loopback-only channel)', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    // Agent output is injectable, so the chat-feed channel refuses external
    // hosts outright — no card, no navigation, and a null return.
    let ret: string | null = 'sentinel'
    act(() => { ret = setSessionPreviewPending('sess-1', 'https://example.com/evil') })
    expect(ret).toBeNull()
    expect(screen.queryByText('Preview ready')).toBeNull()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    // Loopback (incl. *.localhost) still accepted.
    act(() => { ret = setSessionPreviewPending('sess-1', 'http://myapp.localhost:5173') })
    expect(ret).not.toBeNull()
    expect(screen.getByText('Preview ready')).toBeInTheDocument()
  })

  it('reports a target that is this gateway instead of framing a blank page', () => {
    // The dashboard's own origin can never render in the iframe: the gateway
    // sends frame-ancestors 'self' and the cookie-isolation host swap makes the
    // frame cross-origin by construction. The liveness probe can't see that
    // (the server IS up), so the panel has to say so itself.
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: `${window.location.host}/api/hooks/agent` } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.getByText("Can't preview this dashboard here")).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    // The target is still named, and an escape hatch is offered.
    expect(screen.getByText(/\/api\/hooks\/agent/)).toBeInTheDocument()
    expect(screen.getByText('Open in browser')).toBeInTheDocument()
  })

  it('shows a mixed-content URL separately from the consistent browser action', () => {
    const originalHref = window.location.href
    window.location.href = 'https://dashboard.example.com/'
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      const input = screen.getByLabelText('Preview URL')
      fireEvent.change(input, { target: { value: 'http://localhost:5173/very/long/path' } })
      fireEvent.submit(input.closest('form') as HTMLFormElement)

      const shown = 'http://localhost:5173/very/long/path'
      expect(screen.getByText("Can't embed an http:// page here")).toBeInTheDocument()
      expect(screen.getByText(shown).tagName).toBe('CODE')
      expect(screen.getByText('Open in browser').closest('a')).toHaveAttribute('href', shown)
      expect(screen.queryByText(`Open ${shown}`)).toBeNull()
    } finally {
      window.location.href = originalHref
    }
  })

  it('still frames an ordinary dev server on another port', () => {
    // Guards the port comparison: only the gateway's own port is refused.
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'localhost:5173' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.queryByText("Can't preview this dashboard here")).toBeNull()
    expect(screen.getByTitle('Web preview')).toBeInTheDocument()
  })

  it('shows a self-origin target exactly as entered, not host-swapped', () => {
    // The cookie-isolation swap protects a FRAMED server. This target is never
    // framed, so swapping it would tell someone who typed the dashboard's own
    // host that the OTHER loopback alias "is the dashboard's own server" while
    // the panel is explaining itself. Typing the dashboard's host is what
    // triggers the swap, so that is the case this pins.
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    const typed = `${window.location.host}/api/hooks/agent`
    fireEvent.change(input, { target: { value: typed } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.getByText("Can't preview this dashboard here")).toBeInTheDocument()
    const shown = `http://${typed}`
    expect(screen.getByText(shown)).toBeInTheDocument()
    expect((input as HTMLInputElement).value).toBe(shown)
    // The escape hatch points at what they typed too.
    expect(screen.getByText('Open in browser').closest('a')).toHaveAttribute('href', shown)
  })

  it('still cookie-isolates an ordinary dev server it WILL frame', () => {
    // Guards the narrowness of the exemption: only the never-framed self target
    // keeps its host.
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'localhost:5173' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:5173/`)
  })

  it('refuses a chat-fed URL that points back at this gateway', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    // An agent discussing webhooks mentions the gateway's own URL in prose; the
    // offer must not be raised, since Load could only lead to a blank panel.
    let ret: string | null = 'sentinel'
    act(() => { ret = setSessionPreviewPending('sess-1', `http://${window.location.host}/api/hooks/agent`) })
    expect(ret).toBeNull()
    expect(screen.queryByText('Preview ready')).toBeNull()
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('dismisses a pending feed without navigating', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewPending('sess-1', 'localhost:8080') })
    fireEvent.click(screen.getByText('Dismiss'))
    expect(screen.queryByText('Preview ready')).toBeNull()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('shows a "not reachable" state after the dev server stops responding, then auto-restores', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockRejectedValue(new Error('refused'))
    vi.stubGlobal('fetch', fetchMock)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      fireEvent.click(screen.getByText(':3000'))
      expect(screen.getByTitle('Web preview')).toBeInTheDocument()   // loaded initially
      // Two consecutive failed probes (immediate + interval) → unreachable; the
      // stale iframe is unmounted in favor of the stopped state.
      await act(async () => { await vi.advanceTimersByTimeAsync(11000) })
      expect(screen.getByText('Preview server not reachable')).toBeInTheDocument()
      expect(screen.queryByTitle('Web preview')).toBeNull()
      // Server comes back → a successful probe auto-restores the iframe.
      fetchMock.mockResolvedValue(undefined)
      await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
      expect(screen.getByTitle('Web preview')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })


  it('varies the iframe src on Reload so the remount is a new request, not a cache hit', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    const before = (screen.getByTitle('Web preview') as HTMLIFrameElement).src
    fireEvent.click(screen.getByLabelText('Reload preview'))
    const after = (screen.getByTitle('Web preview') as HTMLIFrameElement).src
    // A remount alone re-requests an identical URL, which the browser may answer
    // from cache — the src must actually differ for Reload to mean anything.
    expect(after).not.toBe(before)
    // ...while still pointing at the same server/page.
    expect(targetOf(screen.getByTitle('Web preview') as HTMLIFrameElement))
      .toBe(`http://${iso('localhost')}:3000/`)
  })

  it('keeps the URL pristine on the initial mount-restored load (no stray param)', () => {
    localStorage.setItem('mc-webpreview-url:sess-1', 'http://localhost:4321/')
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.src).toBe(`http://${iso('localhost')}:4321/`)
    expect(frame.src).not.toContain('_kcreload')
  })

  it('leaves the URL bar and the open-in-browser link on the clean URL', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    fireEvent.click(screen.getByLabelText('Reload preview'))
    const clean = `http://${iso('localhost')}:3000/`
    // The cache-buster is an implementation detail of the frame load: it must not
    // leak into what the user sees, copies, or opens externally.
    expect((screen.getByLabelText('Preview URL') as HTMLInputElement).value).toBe(clean)
    expect((screen.getByLabelText('Open in browser') as HTMLAnchorElement).href).toBe(clean)
  })

  it('probes liveness against the clean URL, not the cache-busted one', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    fireEvent.click(screen.getByLabelText('Reload preview'))
    const probed = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map(c => c[0])
    expect(probed.length).toBeGreaterThan(0)
    for (const u of probed) expect(String(u)).not.toContain('_kcreload')
  })

  it('constrains the iframe to a device size when a mobile preset is picked', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    let frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.style.width).toBe('')
    // Device presets now live inside the overflow menu → Preview size submenu.
    // Radix opens on pointerDown, not click.
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'More actions' }),
      { pointerId: 1, button: 0, ctrlKey: false, isPrimary: true },
    )
    fireEvent.click(screen.getByText('Preview size'))
    fireEvent.click(screen.getByText('iPhone SE'))
    frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.style.width).toBe('375px')
    expect(frame.style.height).toBe('667px')
  })

  it('device preset items carry menuitemradio semantics with aria-checked', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'More actions' }),
      { pointerId: 1, button: 0, ctrlKey: false, isPrimary: true },
    )
    fireEvent.click(screen.getByText('Preview size'))
    const responsive = screen.getByRole('menuitemradio', { name: /Responsive/ })
    expect(responsive).toHaveAttribute('aria-checked', 'true')
    const iphone = screen.getByRole('menuitemradio', { name: /iPhone SE/ })
    expect(iphone).toHaveAttribute('aria-checked', 'false')
  })

  it('action row renders exactly 4 top-level sibling controls (regression guard for max-two-buttons-per-row)', () => {
    // The row is: back, forward, (URL container), expand, divider, overflow, crop.
    // The URL container is its own group (not counted); the divider is decorative.
    // Sibling action controls = back + forward + expand + overflow + crop = 5.
    // This matches the base-branch count (back + forward + expand + preview-size
    // + crop = 5). The overflow REPLACED preview-size, so the count did not grow.
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const form = screen.getByLabelText('Preview URL').closest('form') as HTMLFormElement
    // Count direct-child buttons that are action controls (exclude the
    // URL container's internal reload/open-in-browser and the decorative divider).
    const directButtons = Array.from(form.children).filter(el => {
      if (el.getAttribute('aria-hidden') === 'true') return false // divider
      if (el.tagName === 'DIV') return false // URL container
      return el.tagName === 'BUTTON' || el.getAttribute('role') === 'button'
    })
    // 5 = back, forward, expand, overflow trigger, crop (canSnip is mocked on).
    // Identical to baseline (which had preview-size instead of overflow).
    expect(directButtons).toHaveLength(5)
  })

  it('browser-view toggle still works through the overflow menu', () => {
    getBrowserView.mockResolvedValue(STOPPED)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'More actions' }),
      { pointerId: 1, button: 0, ctrlKey: false, isPrimary: true },
    )
    const item = screen.getByRole('menuitem', { name: /Browser view/ })
    expect(item).toBeInTheDocument()
  })

  it('dispatches a snip request when the crop button is clicked', () => {
    let fired = false
    const handler = () => { fired = true }
    window.addEventListener('kirocrew-web-preview-snip', handler)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      fireEvent.click(screen.getByLabelText('Screenshot an area into the chat'))
      expect(fired).toBe(true)
    } finally {
      window.removeEventListener('kirocrew-web-preview-snip', handler)
    }
  })

  it('broadcasts preview-expand true/false as the expand button toggles', () => {
    const seen: boolean[] = []
    const handler = (e: Event) => seen.push(!!(e as CustomEvent<{ expanded?: boolean }>).detail?.expanded)
    window.addEventListener('kirocrew-preview-expand', handler)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      fireEvent.click(screen.getByLabelText('Expand preview'))
      expect(seen).toContain(true)
      fireEvent.click(screen.getByLabelText('Collapse'))
      expect(seen).toContain(false)
    } finally {
      window.removeEventListener('kirocrew-preview-expand', handler)
    }
  })
})

describe('WebPreviewPanel — Playwright CLI browser view', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(undefined))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  /** The toggle, whichever of its two instances is currently exposed (the hidden
   *  subtree is `aria-hidden`, so a role query can only ever see one).
   *
   *  In the URL bar it now sits behind the standard overflow trigger: that row
   *  already carried back / forward / expand / preview-size, and
   *  `max-two-buttons-per-row` forbids a legacy-status row from growing. The
   *  overlay header keeps its own direct button, so when the view is UP the
   *  toggle is still reachable in one click and this helper finds it there. */
  const toggle = () => {
    const direct = screen.queryByRole('button', { name: 'Browser view' })
    if (direct) return direct
    // Radix opens on pointerdown, not click.
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'More actions' }),
      { pointerId: 1, button: 0, ctrlKey: false, isPrimary: true },
    )
    return screen.getByRole('menuitem', { name: /Browser view/ })
  }

  it('frames the CLI dashboard at the reported URL when the view is running', async () => {
    getBrowserView.mockResolvedValue(RUNNING)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    // Running takes the panel over on its own — the same precedence the frame
    // mirror had, so an agent that starts browsing still surfaces itself.
    const frame = await screen.findByTitle('Live browser session') as HTMLIFrameElement
    expect(frame.src).toBe('http://127.0.0.1:45613/')
    expect(screen.getByText('Browser view')).toBeInTheDocument()
    // Preview subtree stays MOUNTED (hidden) under the overlay so iframe/form
    // state survives — its empty-state node is still in the DOM, just hidden.
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('layers nothing over the frame, so the CLI keeps its remote input', async () => {
    // The CLI dashboard's own mouse/keyboard input IS the control surface. A
    // scrim or hint bar over the frame would swallow exactly those events, so
    // the frame must be the last thing in its container and not be inert.
    getBrowserView.mockResolvedValue(RUNNING)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const frame = await screen.findByTitle('Live browser session')
    const box = frame.parentElement as HTMLElement
    expect(box.lastElementChild).toBe(frame)
    expect(frame.className).not.toContain('pointer-events-none')
    expect(frame.closest('[aria-hidden="true"]')).toBeNull()
  })

  it('offers a start action when the view is stopped, and POSTs it', async () => {
    getBrowserView.mockResolvedValue(STOPPED)
    startBrowserView.mockResolvedValue(RUNNING)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    // Stopped does NOT take the panel over — the preview stays usable — so the
    // state is reached through the toggle.
    fireEvent.click(toggle())
    expect(await screen.findByText('Browser view is not running')).toBeInTheDocument()
    expect(screen.queryByTitle('Live browser session')).toBeNull()
    fireEvent.click(screen.getByText('Start browser view'))
    await waitFor(() => expect(startBrowserView).toHaveBeenCalled())
    // The start response IS the new status, so the frame appears without a
    // second read.
    const frame = await screen.findByTitle('Live browser session') as HTMLIFrameElement
    expect(frame.src).toBe('http://127.0.0.1:45613/')
  })

  it('reports a failed start instead of leaving the card looking hung', async () => {
    getBrowserView.mockResolvedValue(STOPPED)
    startBrowserView.mockRejectedValue(new Error('port 45613 already in use'))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(toggle())
    fireEvent.click(await screen.findByText('Start browser view'))
    expect(await screen.findByText(/Couldn't start the browser view/)).toBeInTheDocument()
    expect(screen.getByText(/port 45613 already in use/)).toBeInTheDocument()
  })

  it('reports a start that answered 200 and still did not run', async () => {
    // The endpoint returns the post-attempt STATUS, not an HTTP error, so a launch
    // that failed comes back as a perfectly good `stopped`. Re-rendering the same
    // card would make the click look like it did nothing.
    getBrowserView.mockResolvedValue(STOPPED)
    startBrowserView.mockResolvedValue(STOPPED)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(toggle())
    fireEvent.click(await screen.findByText('Start browser view'))
    expect(await screen.findByText(/Couldn't start the browser view/)).toBeInTheDocument()
    expect(screen.getByText('Browser view is not running')).toBeInTheDocument()
  })

  it('shows the server’s own reason when the view is unavailable', async () => {
    getBrowserView.mockResolvedValue({
      status: 'unavailable',
      url: null,
      port: null,
      reason: 'playwright-cli is not installed. Run: npm install -g @playwright/cli@latest',
    })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(toggle())
    expect(await screen.findByText('Browser view unavailable')).toBeInTheDocument()
    // Verbatim, never translated — it names the real cause, and a catalog key
    // could only either drop the detail or assert a cause the server didn't give.
    expect(screen.getByText(/npm install -g @playwright\/cli@latest/)).toBeInTheDocument()
    expect(screen.queryByTitle('Live browser session')).toBeNull()
  })

  it('falls back to its own copy when an unavailable view reports no reason', async () => {
    getBrowserView.mockResolvedValue({ status: 'unavailable', url: null, port: null, reason: null })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(toggle())
    expect(await screen.findByText('Browser view unavailable')).toBeInTheDocument()
    expect(screen.getByText("The browser view isn't available on this gateway.")).toBeInTheDocument()
  })

  it('explains a running status whose URL cannot be framed rather than navigating it', async () => {
    // A malformed or non-http(s) URL is never handed to the iframe: it would be
    // a navigation the panel cannot vouch for. It degrades to the explanatory
    // state, which is also what makes the value safe to trust elsewhere.
    getBrowserView.mockResolvedValue({
      status: 'running', url: 'javascript:alert(1)', port: 45613, reason: null,
    })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(toggle())
    expect(await screen.findByText('Browser view unavailable')).toBeInTheDocument()
    expect(screen.queryByTitle('Live browser session')).toBeNull()
  })

  it('lets the user dismiss a running view to get the dev-server preview back', async () => {
    getBrowserView.mockResolvedValue(RUNNING)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByTitle('Live browser session')
    // The toggle exposed while the overlay is up is the pressed one in its header.
    fireEvent.click(toggle())
    await waitFor(() => expect(screen.queryByTitle('Live browser session')).toBeNull())
    // ...and the preview is interactive again (no longer under an aria-hidden lid).
    expect(screen.getByLabelText('Preview URL').closest('[aria-hidden="true"]')).toBeNull()
  })
})

describe('WebPreviewPanel — native browser transport', () => {
  // A minimal window.browserAPI bridge, enough for useNativeBrowser to report
  // available:true and (via getState open:true) hand the panel to the native
  // surface. The native view paints outside the DOM, so these assert transport
  // SELECTION + control wiring, never pixels.
  function installNativeBridge(open = true, url = 'https://example.com/') {
    const api = {
      open: vi.fn(async (_p: string, u: string) => ({ open: true, visible: true, url: u, bounds: null })),
      navigate: vi.fn(async (_p: string, u: string) => ({ open: true, visible: true, url: u, bounds: null })),
      setBounds: vi.fn(async () => ({ open, visible: true, url, bounds: null })),
      setOverlayActive: vi.fn(async () => ({ open, visible: true, url, bounds: null })),
      close: vi.fn(async () => ({ open: false, visible: false, url: '', bounds: null })),
      setInactive: vi.fn(async () => ({ open, visible: true, url, bounds: null })),
      getState: vi.fn(async () => ({ open, visible: true, url, bounds: null })),
      setAgentAct: vi.fn(async () => ({ ok: true })),
      setControlOwner: vi.fn(async (_p: string, owner: string) => ({ owner, changed: true })),
      onDidNavigate: vi.fn(() => () => {}),
      onTitleUpdated: vi.fn(() => () => {}),
    }
    ;(window as unknown as { browserAPI?: unknown }).browserAPI = api
    return api
  }

  beforeEach(() => {
    localStorage.clear()
    // The annotate mirror persists per slot in sessionStorage; tests reuse slot ids.
    sessionStorage.clear()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(undefined))
    class RO { observe() {} unobserve() {} disconnect() {} }
    ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RO
  })
  afterEach(() => {
    delete (window as unknown as { browserAPI?: unknown }).browserAPI
    vi.unstubAllGlobals()
  })

  it('native view OWNS the panel when available — a chat-opened page lands in it, not the CLI view', async () => {
    getBrowserView.mockResolvedValue(RUNNING)
    installNativeBridge(true)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    // Wait for the CLI frame to be gone, which only happens once the native view
    // is open and owning the panel. Before getState resolves, a running CLI view
    // legitimately shows -- that is the deliberate fallback for a desktop shell
    // whose native view has nothing in it yet.
    await waitFor(() => expect(screen.queryByTitle('Live browser session')).toBeNull())
    expect(screen.queryByTitle('Live browser session')).toBeNull()
  })

  it('shows the CLI browser view when the bridge EXISTS but no native view is open yet', async () => {
    // Regression: gating this on `!native.available` blanked the panel on a
    // desktop shell whose preload bridge exists while nothing has been opened
    // natively. The real condition is `!nativeOpen`.
    getBrowserView.mockResolvedValue(RUNNING)
    installNativeBridge(false)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await waitFor(() =>
      expect(screen.getByTitle('Live browser session')).toBeInTheDocument()
    )
  })

  it('falls back to the CLI browser view when NO native view is available (remote gateway / plain browser)', async () => {
    // No browserAPI bridge → native.available is false → the CLI dashboard is
    // the only transport, so its frame shows.
    getBrowserView.mockResolvedValue(RUNNING)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    expect(await screen.findByTitle('Live browser session')).toBeInTheDocument()
  })

  // NOTE: three tests are deliberately gone from here — they pinned the
  // per-session consent model this panel no longer has: the Globe toggle
  // acquiring/releasing LIGHT, the BROWSE_MODE_REQUEST_EVENT pull handshake, and
  // the slot filter on its answer. Browser Mode is now the authorization
  // (security.py: "Presence alone is the authorization") and the agent command
  // channel takes LIGHT itself, so the panel carries no agent-authorization
  // control to assert. Transport selection is still covered by the three tests
  // above (native owns the panel / CLI view before a native view / CLI view when
  // no bridge exists).

  it('hides (never destroys) the native view when the panel goes inactive, and closes it on unmount', async () => {
    const api = installNativeBridge(true)
    const { rerender, unmount } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    await waitFor(() => expect(api.getState).toHaveBeenCalled())
    // Inactive → setInactive(true) HIDES; close() must not fire.
    rerender(<WebPreviewPanel sessionKey="sess-1" active={false} />)
    await waitFor(() => expect(api.setInactive.mock.calls.at(-1)?.[1]).toBe(true))
    expect(api.close).not.toHaveBeenCalled()
    // Unmount → close() DESTROYS.
    unmount()
    await waitFor(() => expect(api.close).toHaveBeenCalledWith('sess-1'))
  })

  // ── Annotate: element notes on the live page ──
  // The pick overlay lives in the page; the panel mirrors its picks through
  // the bridge's `annotate` op and OWNS the notes (typed in the panel, never in
  // the page). These assert the mirror, the editor and the hand-off -- never
  // pixels.

  const TARGET = {
    id: 1, n: 1, ref: 'e12', tag: 'button', role: 'button', name: 'Save', text: 'Save',
    selector: 'form > footer > button.primary', detached: false,
  }
  function installAnnotateBridge(items: typeof TARGET[] = [], picked?: number) {
    const api = installNativeBridge(true, 'https://example.com/settings') as Record<string, unknown>
    const state: { items: typeof TARGET[]; picking: boolean; picked?: number; edit?: number } = { items, picking: true, picked }
    const annotate = vi.fn(async (_p: string, op: string, args?: Record<string, unknown>) => {
      switch (op) {
        case 'start': return { ok: true, url: 'https://example.com/settings', title: 'Settings' }
        case 'poll': {
          const out: Record<string, unknown> = { ok: true, picking: state.picking, url: 'https://example.com/settings', title: 'Settings', items: state.items }
          if (state.picked !== undefined) { out.picked = state.picked; state.picked = undefined }
          if (state.edit !== undefined) { out.edit = state.edit; state.edit = undefined }
          return out
        }
        case 'stop': state.picking = false; return { ok: true }
        case 'remove': state.items = state.items.filter(i => i.id !== args?.id); return { ok: true }
        case 'clear': state.items = []; return { ok: true }
        case 'capture': return { ok: true, png: btoa('png'), url: 'https://example.com/settings', title: 'Settings' }
        default: return { ok: true }
      }
    })
    api.annotate = annotate
    return { api, annotate, state }
  }
  /** Pick TARGET (as the page would) and type its note in the panel editor. */
  async function pickAndNote(state: { items: typeof TARGET[]; picked?: number }, note: string, target = TARGET) {
    state.items = [...state.items, target]
    state.picked = target.id
    const input = await screen.findByTestId('browser-annotation-note-input')
    await waitFor(() => expect(input).toHaveFocus())
    fireEvent.change(input, { target: { value: note } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.getByTestId('browser-annotations')).toHaveTextContent(note))
  }

  it('shows Annotate only when a native view is open AND the shell exposes the annotate bridge', async () => {
    installAnnotateBridge()
    const { unmount } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    expect(await screen.findByTestId('browser-annotate')).toHaveTextContent('Annotate')
    unmount()
    installNativeBridge(true) // older shell: no `annotate`
    renderWithProviders(<WebPreviewPanel sessionKey="sess-2" active />)
    await waitFor(() => expect(screen.queryByTitle('Live browser session')).toBeNull())
    expect(screen.queryByTestId('browser-annotate')).toBeNull()
  })

  it('clicking Annotate starts pick mode (badge hint localized, numbering from 0), flips to Done, and Done stops', async () => {
    const { annotate } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await waitFor(() => expect(annotate).toHaveBeenCalledWith('sess-1', 'start', { editHint: expect.any(String), roleNames: expect.objectContaining({ combobox: 'dropdown', textbox: 'text field' }), seq: 0, idStart: 0 }))
    await waitFor(() => expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Done annotating'))
    expect(screen.getByTestId('browser-annotate')).toHaveAttribute('aria-pressed', 'true')
    // Empty state explains the gesture while nothing has been picked yet.
    expect(await screen.findByTestId('browser-annotations')).toHaveTextContent(/Click an element on the page/)
    fireEvent.click(screen.getByTestId('browser-annotate'))
    await waitFor(() => expect(annotate).toHaveBeenCalledWith('sess-1', 'stop', undefined))
  })

  it('a pick on the page opens the note editor here, focused; Enter keeps the note in the panel (never sent to the page)', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    const row = screen.getByTestId('browser-annotations').querySelector('li')!
    expect(row).toHaveTextContent('button "Save"')
    expect(row).toHaveTextContent('too far right')
    expect(screen.queryByTestId('browser-annotation-note-input')).toBeNull()
    // The note text never crossed the bridge in any op.
    for (const call of annotate.mock.calls) expect(JSON.stringify(call)).not.toContain('too far right')
  })

  it('Esc on a fresh pick with nothing typed removes the pick on the page; a marker click (edit) reopens the editor prefilled', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    state.items = [TARGET]
    state.picked = 1
    const input = await screen.findByTestId('browser-annotation-note-input')
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(annotate).toHaveBeenCalledWith('sess-1', 'remove', { id: 1 }))
    // Now a noted pick; the page reports a marker click.
    const t2 = { ...TARGET, id: 2, n: 2, ref: 'e4', tag: 'input', role: 'combobox', name: 'Search', text: '' }
    await pickAndNote(state, 'change placeholder', t2)
    state.edit = 2
    const again = await screen.findByTestId('browser-annotation-note-input')
    expect(again).toHaveValue('change placeholder')
    // The row speaks plainly: the opaque ARIA role is shown as "dropdown" (the draft keeps the raw role).
    expect(screen.getByTestId('browser-annotation-editing')).toHaveTextContent('dropdown "Search"')
  })

  it('Add to chat captures the page with markers and hands ChatPage a draft + the PNG for THIS slot; only noted picks go, notes stay', async () => {
    const { annotate, state } = installAnnotateBridge()
    const seen: CustomEvent[] = []
    const onEv = (e: Event) => { seen.push(e as CustomEvent) }
    window.addEventListener('kirocrew-web-preview-annotate', onEv)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
      fireEvent.click(await screen.findByTestId('browser-annotate'))
      await screen.findByTestId('browser-annotations')
      const send = screen.getByTestId('browser-annotations-send')
      expect(send).toBeDisabled()
      await pickAndNote(state, 'too far right')
      // A second pick left without a note must not appear in the draft.
      state.items = [...state.items, { ...TARGET, id: 2, n: 2, ref: 'e9', name: 'Cancel' }]
      await waitFor(() => expect(screen.getByTestId('browser-annotations').querySelectorAll('li')).toHaveLength(2))
      await waitFor(() => expect(send).not.toBeDisabled())
      fireEvent.click(send)
      await waitFor(() => expect(seen).toHaveLength(1))
      const d = seen[0].detail as { slot: string; files: File[]; draft: string }
      expect(d.slot).toBe('sess-1')
      expect(d.files).toHaveLength(1)
      expect(d.files[0].name).toMatch(/^browser-annotations-.+\.png$/)
      expect(d.draft.split('\n')[0]).toBe('Notes on the page shown in the Browser panel:')
      expect(d.draft).toContain('1. (e12) -- too far right')
      expect(d.draft).toContain('Page: Settings -- https://example.com/settings')
      expect(d.draft).toContain('e12: button "Save" -- form > footer > button.primary')
      expect(d.draft).not.toContain('Cancel')
      expect(d.draft).toContain(d.files[0].name)
      expect(annotate).toHaveBeenCalledWith('sess-1', 'capture', undefined)
      await waitFor(() => expect(annotate).toHaveBeenCalledWith('sess-1', 'stop', undefined))
      expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right')
    } finally {
      window.removeEventListener('kirocrew-web-preview-annotate', onEv)
    }
  })

  it('keeps noted picks (detached) when the page navigated away; Add to chat works without a screenshot; re-Annotate continues numbering and keeps them', async () => {
    const { annotate, state } = installAnnotateBridge()
    const seen: CustomEvent[] = []
    const onEv = (e: Event) => { seen.push(e as CustomEvent) }
    window.addEventListener('kirocrew-web-preview-annotate', onEv)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
      fireEvent.click(await screen.findByTestId('browser-annotate'))
      await screen.findByTestId('browser-annotations')
      await pickAndNote(state, 'too far right')
      // The document navigated: the overlay is gone with it.
      const impl = annotate.getMockImplementation()!
      annotate.mockImplementation(async (p: string, op: string, args?: Record<string, unknown>) => op === 'poll'
        ? { ok: false, code: 'no_overlay', error: 'no annotate overlay on this page' }
        : impl(p, op, args))
      await waitFor(() => expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Annotate'))
      const list = screen.getByTestId('browser-annotations')
      expect(list).toHaveTextContent('too far right')
      expect(list).toHaveTextContent(/The page changed since these notes were made/)
      fireEvent.click(screen.getByTestId('browser-annotations-send'))
      await waitFor(() => expect(seen).toHaveLength(1))
      const d = seen[0].detail as { files: File[]; draft: string }
      expect(d.files).toHaveLength(0)
      expect(d.draft).toContain('(element no longer on the page)')
      expect(annotate).not.toHaveBeenCalledWith('sess-1', 'capture', undefined)
      // Re-entering pick mode on the new page continues numbering after the
      // retained note and does NOT wipe it when the fresh overlay polls empty.
      annotate.mockImplementation(impl)
      state.items = []
      fireEvent.click(screen.getByTestId('browser-annotate'))
      await waitFor(() => expect(annotate).toHaveBeenCalledWith('sess-1', 'start', { editHint: expect.any(String), roleNames: expect.any(Object), seq: 1, idStart: 1 }))
      await waitFor(() => expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Done annotating'))
      await new Promise(r => setTimeout(r, 400))
      expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right')
    } finally {
      window.removeEventListener('kirocrew-web-preview-annotate', onEv)
    }
  })

  it('a live pick whose id collides with a retained note is ignored, never aliasing the note', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    const impl = annotate.getMockImplementation()!
    annotate.mockImplementation(async (p: string, op: string, args?: Record<string, unknown>) => op === 'poll'
      ? { ok: false, code: 'no_overlay', error: 'no annotate overlay on this page' }
      : impl(p, op, args))
    await waitFor(() => expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Annotate'))
    // The page comes back forging a pick with the retained note's id.
    annotate.mockImplementation(impl)
    state.items = [{ ...TARGET, id: 1, n: 7, ref: 'e77', name: 'Impostor' }]
    fireEvent.click(screen.getByTestId('browser-annotate'))
    await waitFor(() => expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Done annotating'))
    await new Promise(r => setTimeout(r, 400))
    const list = screen.getByTestId('browser-annotations')
    expect(list.querySelectorAll('li')).toHaveLength(1)
    expect(list).toHaveTextContent('too far right')
    expect(list).not.toHaveTextContent('Impostor')
  })

  it('a transient poll failure does not drop the notes; three in a row surface an error and pause', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    annotate.mockImplementation(async (_p: string, op: string) => op === 'poll'
      ? { ok: false, code: 'annotate_timeout', error: 'annotate poll timed out after 8000ms' }
      : { ok: true })
    await waitFor(() => expect(screen.getByText('annotate poll timed out after 8000ms')).toBeInTheDocument())
    expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right')
    // Nothing was detached, so the "page changed" notice must not misdiagnose next to the real error.
    expect(screen.getByTestId('browser-annotations')).not.toHaveTextContent(/The page changed since these notes were made/)
    // Resuming after the pause must not start from an empty page: the overlay
    // is still there and so are the notes.
    annotate.mockImplementation(async (_p: string, op: string) => op === 'poll'
      ? { ok: true, picking: true, url: 'https://example.com/settings', title: 'Settings', items: state.items }
      : op === 'start' ? { ok: true, url: 'https://example.com/settings', title: 'Settings' } : { ok: true })
    fireEvent.click(screen.getByTestId('browser-annotate'))
    await waitFor(() => expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Done annotating'))
    await new Promise(r => setTimeout(r, 400))
    expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right')
  })

  it('a refused overlay removal keeps the note (nothing vanishes from here before the page confirms)', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    const impl = annotate.getMockImplementation()!
    annotate.mockImplementation(async (p: string, op: string, args?: Record<string, unknown>) => op === 'remove'
      ? { ok: false, code: 'annotate_failed', error: 'page refused' }
      : impl(p, op, args))
    fireEvent.click(screen.getByRole('button', { name: 'Remove note 1' }))
    await waitFor(() => expect(screen.getByText('page refused')).toBeInTheDocument())
    expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right')
  })

  it('a pick abandoned with nothing typed is dropped when the next element is picked (list, count and draft agree)', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    state.items = [TARGET]
    state.picked = 1
    await screen.findByTestId('browser-annotation-note-input')
    const t2 = { ...TARGET, id: 2, n: 2, ref: 'e4', tag: 'input', role: 'combobox', name: 'Search', text: '' }
    state.items = [TARGET, t2]
    state.picked = 2
    await waitFor(() => expect(annotate).toHaveBeenCalledWith('sess-1', 'remove', { id: 1 }))
    await waitFor(() => expect(screen.getByTestId('browser-annotations').querySelectorAll('li')).toHaveLength(1))
    expect(screen.getByTestId('browser-annotations')).toHaveTextContent('1 note')
  })

  it('a noted pick the page stops reporting is kept as detached rather than losing its note', async () => {
    const { state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    // A tampered reply omits the pick without any remove/clear from the user.
    state.items = []
    await new Promise(r => setTimeout(r, 400))
    expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right')
  })

  it('a destroyed view (no_view) is treated like a vanished overlay: notes kept as detached', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    annotate.mockImplementation(async (_p: string, op: string) => op === 'poll'
      ? { ok: false, code: 'no_view', error: 'the browser view is gone' }
      : { ok: true })
    await waitFor(() => expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Annotate'))
    const list = screen.getByTestId('browser-annotations')
    expect(list).toHaveTextContent('too far right')
    expect(list).toHaveTextContent(/The page changed since these notes were made/)
  })

  it('picking another element while a note is half-typed keeps that draft as the first pick\'s note', async () => {
    const { state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    state.items = [TARGET]
    state.picked = 1
    const input = await screen.findByTestId('browser-annotation-note-input')
    fireEvent.change(input, { target: { value: 'half a thought' } })
    const t2 = { ...TARGET, id: 2, n: 2, ref: 'e4', tag: 'input', role: 'combobox', name: 'Search', text: '' }
    state.items = [TARGET, t2]
    state.picked = 2
    await waitFor(() => expect(screen.getByTestId('browser-annotation-editing')).toHaveTextContent('dropdown "Search"'))
    const rows = screen.getByTestId('browser-annotations').querySelectorAll('li')
    expect(rows[0]).toHaveTextContent('half a thought')
  })

  it('Clear asks once (restating the count) before wiping typed notes', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(annotate).not.toHaveBeenCalledWith('sess-1', 'clear', undefined)
    fireEvent.click(screen.getByRole('button', { name: 'Clear 1 note?' }))
    await waitFor(() => expect(annotate).toHaveBeenCalledWith('sess-1', 'clear', undefined))
  })

  it('never shows or sends another session\'s notes after a session switch, and finds them again on return', async () => {
    const { annotate, state } = installAnnotateBridge()
    const { rerender, unmount } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    rerender(<WebPreviewPanel sessionKey="sess-2" active />)
    // Synchronously after the switch: the old slot's mirror is not rendered.
    expect(screen.queryByTestId('browser-annotations')).toBeNull()
    // A switch is not a teardown: the other session's view (and overlay) stays alive.
    expect(annotate.mock.calls.some(c => c[1] === 'teardown')).toBe(false)
    rerender(<WebPreviewPanel sessionKey="sess-1" active />)
    await waitFor(() => expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right'))
    // Only the panel closing tears the live overlays down.
    unmount()
    expect(annotate.mock.calls.some(c => c[0] === 'sess-1' && c[1] === 'teardown')).toBe(true)
  })

  it('Done annotating → Annotate again keeps every pick and note', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    fireEvent.click(screen.getByTestId('browser-annotate')) // Done picking
    await waitFor(() => expect(annotate).toHaveBeenCalledWith('sess-1', 'stop', undefined))
    fireEvent.click(screen.getByTestId('browser-annotate')) // Annotate again
    await waitFor(() => expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Done annotating'))
    await new Promise(r => setTimeout(r, 400))
    expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right')
    expect(screen.getByTestId('browser-annotations-send')).not.toBeDisabled()
  })

  it('a note still being typed when the page navigates away is kept, not lost', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    state.items = [TARGET]
    state.picked = 1
    const input = await screen.findByTestId('browser-annotation-note-input')
    fireEvent.change(input, { target: { value: 'half a thought' } })
    const impl = annotate.getMockImplementation()!
    annotate.mockImplementation(async (p: string, op: string, args?: Record<string, unknown>) => op === 'poll'
      ? { ok: false, code: 'no_overlay', error: 'no annotate overlay on this page' }
      : impl(p, op, args))
    await waitFor(() => expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Annotate'))
    expect(screen.getByTestId('browser-annotations')).toHaveTextContent('half a thought')
  })

  it('survives a dashboard reload: notes are restored from sessionStorage for the same slot', async () => {
    const { state } = installAnnotateBridge()
    const { unmount } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    unmount()
    installAnnotateBridge([TARGET])
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    await waitFor(() => expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right'))
  })

  it('counts what is listed (picks, noted or not) -- the same rows Clear would wipe; only noted picks are sent', async () => {
    const { state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    state.items = [...state.items, { ...TARGET, id: 2, n: 2, ref: 'e9', name: 'Cancel' }]
    await waitFor(() => expect(screen.getByTestId('browser-annotations').querySelectorAll('li')).toHaveLength(2))
    expect(screen.getByTestId('browser-annotations')).toHaveTextContent('2 notes')
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(screen.getByRole('button', { name: 'Clear 2 notes?' })).toBeInTheDocument()
  })

  it('a failed stop is surfaced and leaves pick mode on for the poll to reconcile', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    const impl = annotate.getMockImplementation()!
    annotate.mockImplementation(async (p: string, op: string, args?: Record<string, unknown>) => op === 'stop'
      ? { ok: false, code: 'timeout', error: 'annotate stop timed out after 8000ms' }
      : impl(p, op, args))
    fireEvent.click(screen.getByTestId('browser-annotate'))
    await waitFor(() => expect(screen.getByText('annotate stop timed out after 8000ms')).toBeInTheDocument())
    expect(state.picking).toBe(true)
    expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Done annotating')
  })

  it('Add to chat still drafts when the stop fails, but reports it and leaves pick mode on', async () => {
    const { annotate, state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    const impl = annotate.getMockImplementation()!
    annotate.mockImplementation(async (p: string, op: string, args?: Record<string, unknown>) => op === 'stop'
      ? { ok: false, code: 'timeout', error: 'annotate stop timed out after 8000ms' }
      : impl(p, op, args))
    const seen: Event[] = []
    window.addEventListener('kirocrew-web-preview-annotate', e => seen.push(e))
    fireEvent.click(screen.getByTestId('browser-annotations-send'))
    await waitFor(() => expect(seen).toHaveLength(1))
    await waitFor(() => expect(screen.getByText('annotate stop timed out after 8000ms')).toBeInTheDocument())
    expect(state.picking).toBe(true)
    expect(screen.getByTestId('browser-annotate')).toHaveTextContent('Done annotating')
  })

  it('an armed Clear does not carry over to another session', async () => {
    const { state } = installAnnotateBridge()
    const { rerender } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(screen.getByRole('button', { name: 'Clear 1 note?' })).toBeInTheDocument()
    rerender(<WebPreviewPanel sessionKey="sess-2" active />)
    rerender(<WebPreviewPanel sessionKey="sess-1" active />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Clear 1 note?' })).toBeNull()
  })

  it('Add to chat mid-edit sends the text still open in the editor, not the previously saved note', async () => {
    const { state } = installAnnotateBridge()
    const seen: CustomEvent[] = []
    const onEv = (e: Event) => { seen.push(e as CustomEvent) }
    window.addEventListener('kirocrew-web-preview-annotate', onEv)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
      fireEvent.click(await screen.findByTestId('browser-annotate'))
      await screen.findByTestId('browser-annotations')
      await pickAndNote(state, 'too far right')
      fireEvent.click(screen.getByRole('button', { name: 'Edit note 1' }))
      const input = await screen.findByTestId('browser-annotation-note-input')
      fireEvent.change(input, { target: { value: 'actually too far LEFT' } })
      fireEvent.click(screen.getByTestId('browser-annotations-send'))
      await waitFor(() => expect(seen).toHaveLength(1))
      const d = seen[0].detail as { draft: string }
      expect(d.draft).toContain('-- actually too far LEFT')
      expect(d.draft).not.toContain('too far right')
      expect(screen.getByTestId('browser-annotations')).toHaveTextContent('actually too far LEFT')
    } finally {
      window.removeEventListener('kirocrew-web-preview-annotate', onEv)
    }
  })

  it('Add to chat acknowledges inline that nothing was sent yet', async () => {
    const { state } = installAnnotateBridge()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    fireEvent.click(screen.getByTestId('browser-annotations-send'))
    expect(await screen.findByTestId('browser-annotations-added')).toHaveTextContent(/nothing is sent until you send the message/)
  })
})

describe('WebPreviewPanel — address bar launcher (non-native transport)', () => {
  // No `window.browserAPI` bridge in these tests, so `useNativeBrowser` reports
  // available:false — the plain-browser / remote-gateway transport, where the
  // gateway host's Playwright CLI browser is the only thing that can render an
  // external site.
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(undefined))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  const submit = (raw: string) => {
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: raw } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
  }

  it('drops a launch answer for a slot the user has left, on success and on failure', async () => {
    // Slot A's launch is still in flight when the user switches to slot B; the
    // late answer must not paint B's header, view or failure card.
    let resolveA: (v: unknown) => void = () => {}
    let rejectC: (e: unknown) => void = () => {}
    openInBrowser.mockImplementation((_url: string, sessionKey: string) => new Promise((resolve, reject) => {
      if (sessionKey === 'sess-a') resolveA = resolve
      if (sessionKey === 'sess-c') rejectC = reject
    }))
    const { rerender } = renderWithProviders(<WebPreviewPanel sessionKey="sess-a" />)
    submit('google.com')
    await waitFor(() => expect(openInBrowser).toHaveBeenCalledWith('https://google.com/', 'sess-a'))
    rerender(<WebPreviewPanel sessionKey="sess-b" />)
    await act(async () => { resolveA(OPENED('https://google.com/')) })
    expect(screen.queryByTestId('web-preview-session-name')).toBeNull()
    expect(screen.queryByTitle('Live browser session')).toBeNull()
    expect(screen.queryByText('Opening in the browser…')).toBeNull()
    // Same guard on the error path: a late failure does not paint the new slot's card.
    rerender(<WebPreviewPanel sessionKey="sess-c" />)
    submit('example.com')
    await waitFor(() => expect(openInBrowser).toHaveBeenCalledWith('https://example.com/', 'sess-c'))
    rerender(<WebPreviewPanel sessionKey="sess-d" />)
    await act(async () => { rejectC(new Error('boom')) })
    expect(screen.queryByTestId('web-preview-launch-error')).toBeNull()
  })

  it('paints only the latest launch on a slot: an earlier launch failing late never replaces the newer view', async () => {
    // Mistype, then retype before the first answer lands: A (the typo) is still
    // in flight when B (the corrected address) is submitted and succeeds. A's
    // late failure must not swap B's live view for a stale failure card.
    let answerA: (v: unknown) => void = () => {}
    let answerB: (v: unknown) => void = () => {}
    openInBrowser.mockImplementation((url: string) => new Promise((resolve) => {
      if (url === 'https://gooogle.com/') answerA = resolve
      if (url === 'https://google.com/') answerB = resolve
    }))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('gooogle.com')
    await waitFor(() => expect(openInBrowser).toHaveBeenCalledWith('https://gooogle.com/', 'sess-1'))
    submit('google.com')
    await waitFor(() => expect(openInBrowser).toHaveBeenCalledWith('https://google.com/', 'sess-1'))
    await act(async () => { answerB(OPENED('https://google.com/')) })
    expect(await screen.findByTitle('Live browser session')).toBeInTheDocument()
    expect(screen.getByTestId('web-preview-session-name').textContent).toBe('panel-1234abcd')
    await act(async () => {
      answerA({ ok: false, session: 'panel-1234abcd', error: 'Error: page.goto: net::ERR_NAME_NOT_RESOLVED', view: RUNNING })
    })
    expect(screen.getByTitle('Live browser session')).toBeInTheDocument()
    expect(screen.queryByTestId('web-preview-launch-error')).toBeNull()
    expect(screen.queryByText(/ERR_NAME_NOT_RESOLVED/)).toBeNull()
    // The mirror image: the newer launch's own failure still shows, so a real
    // error is never hidden behind an older success.
    let answerC: (v: unknown) => void = () => {}
    openInBrowser.mockImplementation((url: string) => new Promise((resolve) => {
      if (url === 'https://example.invalid/') answerC = resolve
    }))
    submit('example.invalid')
    await waitFor(() => expect(openInBrowser).toHaveBeenCalledWith('https://example.invalid/', 'sess-1'))
    await act(async () => {
      answerC({ ok: false, session: 'panel-1234abcd', error: 'Error: page.goto: net::ERR_NAME_NOT_RESOLVED', view: RUNNING })
    })
    expect(screen.getByTestId('web-preview-launch-error')).toBeInTheDocument()
  })

  it('sends an external host to the gateway browser and shows the CLI view, never the iframe', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('google.com')
    // Upgraded to https, keyed to THIS chat slot.
    await waitFor(() => expect(openInBrowser).toHaveBeenCalledWith('https://google.com/', 'sess-1'))
    // The preview iframe was never pointed at the external site: no frame, no
    // liveness probe against it (which is what produced "not reachable"). Every
    // probe the panel made went to a loopback host, none to the public site.
    expect(screen.queryByTitle('Web preview')).toBeNull()
    const probed = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
      .map(c => String(c[0]))
      .filter(u => /^https?:\/\//.test(u))  // the api client's own relative /api/… calls are not probes
    for (const u of probed) expect(['127.0.0.1', 'localhost']).toContain(new URL(u).hostname)
    // The answer carried the view status, so the CLI dashboard frames at once.
    const frame = await screen.findByTitle('Live browser session') as HTMLIFrameElement
    expect(frame.src).toBe('http://127.0.0.1:45613/')
    expect(screen.queryByText('Preview server not reachable')).toBeNull()
    // The header names THIS chat's browser by the session name the framed sidebar
    // lists (visible label, not a tooltip), and one line says how the next page is opened.
    expect(screen.getByText("This chat's browser")).toBeInTheDocument()
    expect(screen.getByTestId('web-preview-session-name').textContent).toBe('panel-1234abcd')
    // Narrow widths (320px): the label group is the row's only flexible item and
    // truncates, so the header's controls — the way back to the preview bar —
    // never overflow. jsdom does no layout, so pin the contract on the classes.
    const group = screen.getByTestId('web-preview-session-label')
    expect(group.className).toMatch(/\bmin-w-0\b/)
    expect(group.className).toMatch(/\bflex-1\b/)
    expect(group.className).not.toMatch(/\bshrink-0\b/)
    expect(screen.getByText("This chat's browser").className).toMatch(/\btruncate\b/)
    expect(screen.getByText(/^Click the padlock above the page/)).toBeInTheDocument()
  })

  it('the padlock hint is one sentence and stays dismissed in this browser once dismissed', async () => {
    const { unmount } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('google.com')
    await screen.findByTitle('Live browser session')
    const hint = screen.getByTestId('web-preview-padlock-hint')
    // One sentence: no second full stop before the end.
    expect(hint.textContent?.trim().replace(/\.$/, '')).not.toMatch(/\.\s/)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('web-preview-padlock-hint')).toBeNull()
    // Another chat, or a reload: the dismissal is remembered per browser.
    unmount()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-2" />)
    submit('example.com')
    await screen.findByTitle('Live browser session')
    expect(screen.getByTestId('web-preview-session-name').textContent).toBe('panel-1234abcd')
    expect(screen.queryByTestId('web-preview-padlock-hint')).toBeNull()
  })

  it('names the session to pick only when the framed dashboard did not attach', async () => {
    // The gateway says whether the auto-attach happened. When it did not, the
    // reader is looking at the frame's session grid with no page, so one line
    // names the session to click; when it did, nothing extra is said.
    openInBrowser.mockResolvedValue({ ...OPENED('https://google.com/'), attached: false })
    const { unmount } = renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('google.com')
    await screen.findByTitle('Live browser session')
    const line = screen.getByTestId('web-preview-pick-session')
    expect(line.textContent).toContain('panel-1234abcd')
    expect(screen.queryByTestId('web-preview-padlock-hint')).toBeNull()
    unmount()
    openInBrowser.mockResolvedValue({ ...OPENED('https://google.com/'), attached: true })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-2" />)
    submit('google.com')
    await screen.findByTitle('Live browser session')
    expect(screen.queryByTestId('web-preview-pick-session')).toBeNull()
    expect(screen.getByTestId('web-preview-padlock-hint')).toBeInTheDocument()
  })

  it('shows an opening state while the gateway launches the browser', async () => {
    let resolve!: (v: unknown) => void
    openInBrowser.mockImplementation(() => new Promise(r => { resolve = r }))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('https://example.com/')
    expect(await screen.findByText('Opening in the browser…')).toBeInTheDocument()
    expect(screen.getByText('https://example.com/')).toBeInTheDocument()
    await act(async () => { resolve(OPENED('https://example.com/')) })
    await screen.findByTitle('Live browser session')
    expect(screen.queryByText('Opening in the browser…')).toBeNull()
  })

  it('keeps a loopback dev server on the iframe path and never calls the launcher', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('localhost:8080')
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:8080/`)
    expect(openInBrowser).not.toHaveBeenCalled()
    expect(screen.queryByTitle('Live browser session')).toBeNull()
  })

  it('renders the gateway’s own error text verbatim when the CLI fails, never a blank frame', async () => {
    const text = 'Error: Daemon pid=1467353: Daemon process exited with code 1\n'
      + 'Chromium sandboxing failed!\n'
      + 'No usable sandbox! If you want to live dangerously and need an immediate workaround, you can try using --no-sandbox.\n\n'
      + 'Chromium could not start because this host cannot run its sandbox. Kiro Crew never disables the sandbox by default. '
      + 'To accept that trade-off on this host, point PLAYWRIGHT_MCP_CONFIG in the gateway\'s environment at your own playwright-cli config.'
    openInBrowser.mockResolvedValue({ ok: false, session: 'panel-1234abcd', error: text, view: RUNNING })
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('google.com')
    expect(await screen.findByText("Couldn't open this page in the browser")).toBeInTheDocument()
    // The CLI's words, exactly — a catalog key could only drop the cause or
    // assert one the gateway did not give.
    const notice = screen.getByTestId('web-preview-launch-error')
    expect(notice.textContent).toContain('No usable sandbox!')
    expect(notice.textContent).toContain('PLAYWRIGHT_MCP_CONFIG')
    // Not the dev-server copy, and nothing framed.
    expect(screen.queryByText('Preview server not reachable')).toBeNull()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    expect(screen.queryByTitle('Live browser session')).toBeNull()
    // Retry re-submits the same URL; the notice's own dismiss clears the card.
    // One action in the row -- the third and later would need an overflow.
    const actions = screen.getByText('Try again').closest('button') as HTMLButtonElement
    expect(actions).not.toBeNull()
    fireEvent.click(actions)
    await waitFor(() => expect(openInBrowser).toHaveBeenCalledTimes(2))
    expect(openInBrowser).toHaveBeenLastCalledWith('https://google.com/', 'sess-1')
    fireEvent.click(await screen.findByLabelText('Dismiss'))
    expect(screen.queryByText("Couldn't open this page in the browser")).toBeNull()
  })

  it('reports a transport failure of the launcher request', async () => {
    openInBrowser.mockRejectedValue(new Error('url must be http(s) with a host and no credentials'))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('https://example.com/')
    expect(await screen.findByText("Couldn't open this page in the browser")).toBeInTheDocument()
    expect(screen.getByTestId('web-preview-launch-error').textContent)
      .toContain('url must be http(s) with a host and no credentials')
  })

  it('refuses a ?query or #fragment URL before the round trip as a plain hint pointing at the frame\'s own address bar', async () => {
    // The gateway refuses these shapes (argv is readable by same-host accounts),
    // so the panel does not even ask. Nothing failed, so this is a validation
    // hint in plain text — not an ErrorNotice, no alert role — that says where
    // such a link goes.
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('https://example.com/search?q=hello')
    const hint = await screen.findByTestId('web-preview-launch-refused')
    expect(openInBrowser).not.toHaveBeenCalled()
    expect(hint.textContent).toMatch(/frame's own address bar/)
    expect(hint.textContent).toMatch(/padlock/)
    expect(screen.queryByTestId('web-preview-launch-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText("Couldn't open this page in the browser")).toBeNull()
    // Nothing to retry: the same address would be refused again.
    expect(screen.queryByText('Try again')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('web-preview-launch-refused')).toBeNull()
    submit('https://example.com/docs#install')
    expect(await screen.findByTestId('web-preview-launch-refused')).toBeInTheDocument()
    expect(openInBrowser).not.toHaveBeenCalled()
    // A plain address still goes through.
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    submit('https://example.com/docs')
    await waitFor(() => expect(openInBrowser).toHaveBeenCalledWith('https://example.com/docs', 'sess-1'))
  })

  it('a loopback dev-server URL with a query keeps the iframe path (the refusal is the launcher\'s)', async () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('http://localhost:5173/app?tab=2')
    const frame = await screen.findByTitle('Web preview') as HTMLIFrameElement
    expect(frame.src).toContain('tab=2')
    expect(openInBrowser).not.toHaveBeenCalled()
    expect(screen.queryByTestId('web-preview-launch-error')).toBeNull()
  })

  it('shows the gateway reason for an invalid_url refusal such as URL credentials', async () => {
    // Query/fragment data is caught locally, but userinfo reaches the gateway's
    // independent validator. Its reason must name credentials rather than send
    // the reader looking for query/hash punctuation that is not present.
    const { ApiError } = await import('../api/client')
    openInBrowser.mockRejectedValue(new ApiError(
      400,
      'url must be http(s) with a host and no credentials, query, or fragment (a secret in the URL would leak via argv)',
      JSON.stringify({ error: 'url must be http(s) with a host and no credentials, query, or fragment (a secret in the URL would leak via argv)', code: 'invalid_url' }),
    ))
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    submit('http://user:pass@example.com/')
    // A request went out and was rejected, so this one IS an error surface.
    const card = await screen.findByTestId('web-preview-launch-error')
    expect(card.textContent).toMatch(/no credentials, query, or fragment/)
    expect(card.textContent).not.toMatch(/frame's own address bar/)
    expect(screen.queryByTestId('web-preview-launch-refused')).toBeNull()
    expect(screen.queryByText('Try again')).toBeNull()
  })

  it('adds no second address bar over the CLI view (its own chrome carries navigation)', async () => {
    getBrowserView.mockResolvedValue(RUNNING)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const frame = await screen.findByTitle('Live browser session')
    // The framed dashboard has its own URL bar, tab bar and remote input; a
    // panel-level bar beside it would be a second, disagreeing address bar.
    // Only the (hidden) preview subtree's URL field exists.
    expect(screen.getAllByRole('textbox', { hidden: true })).toHaveLength(1)
    expect(frame.parentElement?.lastElementChild).toBe(frame)
  })

  it('explains an unreachable view (gateway loopback, no forward) instead of framing a dead page', async () => {
    vi.useFakeTimers()
    // The gateway says running — it is, from where IT stands — but from this
    // browser the loopback URL refuses to connect: a laptop on a tunnel without
    // a forward for the view's port.
    const fetchMock = vi.fn(async (input: unknown) => {
      if (String(input).startsWith('http://127.0.0.1:45613')) throw new Error('refused')
    })
    vi.stubGlobal('fetch', fetchMock)
    getBrowserView.mockResolvedValue(RUNNING)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      await act(async () => { await vi.advanceTimersByTimeAsync(50) })
      expect(screen.getByTitle('Live browser session')).toBeInTheDocument()
      // Two failed probes (immediate + one interval) ⇒ the frame yields to the
      // explanation, which names the URL and the setting that fixes it.
      await act(async () => { await vi.advanceTimersByTimeAsync(11000) })
      // Rendered through the shared error surface, naming the URL and the
      // setting that fixes it.
      const notice = screen.getByTestId('web-preview-view-unreachable')
      expect(notice).toHaveAttribute('role', 'alert')
      expect(notice.textContent).toContain("Browser view can't be reached from this browser")
      expect(notice.textContent).toMatch(/pin the view's port/)
      expect(screen.getByText('http://127.0.0.1:45613/')).toBeInTheDocument()
      expect(screen.getByText('dashboard.browser_view_port')).toBeInTheDocument()
      expect(screen.queryByTitle('Live browser session')).toBeNull()
      expect(screen.queryByTestId('web-preview-padlock-hint')).toBeNull()
      // The header dot says what THIS browser sees: not green beside this card.
      expect(screen.getByTestId('web-preview-view-dot').style.backgroundColor).toBe('var(--danger)')
      // The view comes within reach (the forward is up) → a retry restores the frame.
      fetchMock.mockImplementation(async () => undefined)
      fireEvent.click(screen.getByText('Try again'))
      await act(async () => { await vi.advanceTimersByTimeAsync(50) })
      expect(screen.getByTitle('Live browser session')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })
})

describe('WebPreviewPanel — native transport keeps its own path for external hosts', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(undefined))
    class RO { observe() {} unobserve() {} disconnect() {} }
    ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RO
  })
  afterEach(() => {
    delete (window as unknown as { browserAPI?: unknown }).browserAPI
    vi.unstubAllGlobals()
  })

  it('sends an external host to the native view, not to the gateway launcher', async () => {
    const open = vi.fn(async (_p: string, u: string) => ({ open: true, visible: true, url: u, bounds: null }))
    ;(window as unknown as { browserAPI?: unknown }).browserAPI = {
      open,
      navigate: vi.fn(async (_p: string, u: string) => ({ open: true, visible: true, url: u, bounds: null })),
      setBounds: vi.fn(async () => ({ open: false, visible: true, url: '', bounds: null })),
      setOverlayActive: vi.fn(async () => ({ open: false, visible: true, url: '', bounds: null })),
      close: vi.fn(async () => ({ open: false, visible: false, url: '', bounds: null })),
      setInactive: vi.fn(async () => ({ open: false, visible: true, url: '', bounds: null })),
      getState: vi.fn(async () => ({ open: false, visible: true, url: '', bounds: null })),
      setAgentAct: vi.fn(async () => ({ ok: true })),
      setControlOwner: vi.fn(async (_p: string, owner: string) => ({ owner, changed: true })),
      onDidNavigate: vi.fn(() => () => {}),
      onTitleUpdated: vi.fn(() => () => {}),
    }
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'google.com' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    await waitFor(() => expect(open).toHaveBeenCalledWith('sess-1', 'https://google.com/'))
    expect(openInBrowser).not.toHaveBeenCalled()
  })
})
