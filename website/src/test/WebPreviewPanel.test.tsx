import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import WebPreviewPanel, { normalizeUrl, setSessionPreviewUrl, setSessionPreviewPending, isolatePreviewHost, withCacheBuster, PREVIEW_ENABLE_BROWSE_EVENT, BROWSE_MODE_EVENT } from '../components/WebPreviewPanel'

// The crop button is gated on snip support (getDisplayMedia). Force it on so
// the button renders under happy-dom (which has no mediaDevices.getDisplayMedia).
vi.mock('../hooks/useScreenSnip', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useScreenSnip')>()
  return { ...actual, isScreenSnipSupported: () => true }
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
  it('keeps explicit http/https', () => {
    expect(normalizeUrl('https://example.com')).toBe('https://example.com/')
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
    fireEvent.click(screen.getByLabelText('Preview size'))
    fireEvent.click(screen.getByText('iPhone SE'))
    frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.style.width).toBe('375px')
    expect(frame.style.height).toBe('667px')
  })

  it('device preset buttons are type=button so they never submit the URL form', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByLabelText('Preview size'))
    const preset = screen.getByText('iPhone SE').closest('button') as HTMLButtonElement
    expect(preset.getAttribute('type')).toBe('button')
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

  it('broadcasts preview-focus true/false as the expand button toggles', () => {
    const seen: boolean[] = []
    const handler = (e: Event) => seen.push(!!(e as CustomEvent<{ focused?: boolean }>).detail?.focused)
    window.addEventListener('kirocrew-preview-focus', handler)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      fireEvent.click(screen.getByLabelText('Expand preview'))
      expect(seen).toContain(true)
      fireEvent.click(screen.getByLabelText('Collapse'))
      expect(seen).toContain(false)
    } finally {
      window.removeEventListener('kirocrew-preview-focus', handler)
    }
  })
})

describe('WebPreviewPanel — live agent-browse mirror', () => {
  const emitFrame = (session_key = 'sess-1') =>
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-browser-frame', {
        detail: { data: 'Zm9vYmFy', format: 'jpeg', session_key },
      }))
    })

  it('overlays the read-only live mirror when a browse frame arrives (preview stays mounted)', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.queryByText('Browser — live')).toBeNull()
    emitFrame('sess-1')
    expect(screen.getByText('Browser — live')).toBeInTheDocument()
    expect(screen.getByAltText('Live browser session')).toBeInTheDocument()
    // Preview subtree stays MOUNTED (hidden) under the overlay so iframe/form
    // state survives — its empty-state node is still in the DOM, just hidden.
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('offers "Let the agent act" while the toggle is off and requests it for THIS session on click', () => {
    const seen: (string | undefined)[] = []
    const handler = (e: Event) => seen.push((e as CustomEvent<{ slot?: string }>).detail?.slot)
    window.addEventListener(PREVIEW_ENABLE_BROWSE_EVENT, handler)
    try {
      renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
      emitFrame('sess-1')
      fireEvent.click(screen.getByText('Let the agent act'))
      // Grant must be attributed to the panel's own (browsing) session, not a
      // global/active-slot fallback.
      expect(seen).toEqual(['sess-1'])
    } finally {
      window.removeEventListener(PREVIEW_ENABLE_BROWSE_EVENT, handler)
    }
  })

  it('does NOT show the live mirror for a frame from a DIFFERENT session (no cross-session leak)', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-2') // a background session's browse frame
    // This panel is scoped to sess-1, so a sess-2 frame must not flip it live —
    // otherwise "Let the agent act" would authorize the wrong session.
    expect(screen.queryByText('Browser — live')).toBeNull()
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('reflects the toggle ON: shows "Agent can act", hides the enable button', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    expect(screen.getByText('Let the agent act')).toBeInTheDocument()
    act(() => {
      window.dispatchEvent(new CustomEvent(BROWSE_MODE_EVENT, { detail: { on: true } }))
    })
    expect(screen.getByText('Agent can act')).toBeInTheDocument()
    expect(screen.queryByText('Let the agent act')).toBeNull()
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

  const emitFrame = (session_key = 'sess-1') =>
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-browser-frame', {
        detail: { data: 'Zm9vYmFy', format: 'jpeg', session_key },
      }))
    })

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

  it('native view OWNS the panel when available — a chat-opened page lands in it, not the mirror', async () => {
    installNativeBridge(true)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    // NOTE: 'Let the agent act' appears on BOTH surfaces (the native header
    // reuses the mirror's keys), so it cannot distinguish them. Wait instead for
    // the mirror's <img> to be gone, which only happens once the native view is
    // open and owning the panel. Before getState resolves, a streamed frame
    // legitimately shows the mirror -- that is the deliberate fallback for a
    // desktop shell whose native view has nothing in it yet.
    emitFrame('sess-1')
    await waitFor(() => expect(screen.queryByAltText('Live browser session')).toBeNull())
    // A further streamed frame must NOT override the now-open native view.
    emitFrame('sess-1')
    expect(screen.queryByAltText('Live browser session')).toBeNull()
    expect(screen.getByText('Let the agent act')).toBeInTheDocument()
  })

  it('shows the mirror when the bridge EXISTS but no native view is open yet', async () => {
    // Regression: gating the mirror on `!native.available` blanked the panel on a
    // desktop shell whose preload bridge exists while nothing has been opened
    // natively -- frames were arriving with nothing rendering them. The real
    // condition is `!nativeOpen`.
    installNativeBridge(false)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    await waitFor(() =>
      expect(screen.getByAltText('Live browser session')).toBeInTheDocument()
    )
  })

  it('falls back to the read-only mirror when NO native view is available (remote gateway / plain browser)', () => {
    // No browserAPI bridge → native.available is false → streamed frames are the
    // only transport, so the mirror shows.
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    expect(screen.getByAltText('Live browser session')).toBeInTheDocument()
  })

  it('wires the Globe toggle: ON acquires LIGHT control, OFF releases to NONE', async () => {
    const api = installNativeBridge(true)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    await screen.findByText('Let the agent act')
    // Globe ON → mirror agent-act authorization AND request LIGHT.
    act(() => { window.dispatchEvent(new CustomEvent(BROWSE_MODE_EVENT, { detail: { on: true } })) })
    await waitFor(() => expect(api.setControlOwner.mock.calls.at(-1)?.[1]).toBe('light'))
    expect(api.setAgentAct.mock.calls.at(-1)?.[1]).toBe(true)
    // Globe OFF → actively release.
    act(() => { window.dispatchEvent(new CustomEvent(BROWSE_MODE_EVENT, { detail: { on: false } })) })
    await waitFor(() => expect(api.setControlOwner.mock.calls.at(-1)?.[1]).toBe('none'))
    expect(api.setAgentAct.mock.calls.at(-1)?.[1]).toBe(false)
  })

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
})
