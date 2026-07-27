import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

import WebPreviewPanel, { normalizeUrl, setSessionPreviewUrl, setSessionPreviewPending, isolatePreviewHost } from '../components/WebPreviewPanel'

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
    render(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    expect(screen.getByText(':5173')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    // Quick-pick buttons are type=button so a valid draft in the URL field
    // can't be overridden by a stray form submission.
    expect((screen.getByText(':5173').closest('button') as HTMLButtonElement).getAttribute('type')).toBe('button')
  })

  it('loads a typed URL into the iframe (normalizing scheme + isolating host) on submit', () => {
    render(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'localhost:8080' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.src).toBe(`http://${iso('localhost')}:8080/`)
  })

  it('enables back only after navigating to a second URL, and steps back', () => {
    render(<WebPreviewPanel sessionKey="sess-1" />)
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
    expect(frame.src).toBe(`http://${iso('localhost')}:3000/`)
    expect(screen.getByLabelText('Forward')).not.toBeDisabled()
  })

  it('loads a quick-pick port (isolated host)', () => {
    render(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText(':3000'))
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.src).toBe(`http://${iso('localhost')}:3000/`)
  })

  it('persists the URL per session and restores it on mount', () => {
    localStorage.setItem('mc-webpreview-url:sess-1', 'http://localhost:4321/')
    render(<WebPreviewPanel sessionKey="sess-1" />)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.src).toBe(`http://${iso('localhost')}:4321/`)
    render(<WebPreviewPanel sessionKey="sess-2" />)
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('loads a URL fed externally via setSessionPreviewUrl (matching slot, isolated)', () => {
    render(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    act(() => { setSessionPreviewUrl('sess-1', 'localhost:8080') })
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.src).toBe(`http://${iso('localhost')}:8080/`)
  })

  it('does not live-load an external feed when open=false (offer only)', () => {
    render(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewUrl('sess-1', 'localhost:8080', false) })
    // No dispatch → the already-mounted panel stays on the empty state.
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
  })

  it('ignores an external feed aimed at a different slot', () => {
    render(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewUrl('sess-2', 'localhost:8080') })
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
  })

  it('shows a Load-preview card for a pending feed and navigates only on the explicit click', () => {
    render(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewPending('sess-1', 'localhost:8080') })
    // Pending → a card is shown and the iframe is NOT loaded (no auto-GET).
    expect(screen.getByText('Preview ready')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    // Explicit click is what fires the load.
    fireEvent.click(screen.getByText('Load preview'))
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.src).toBe(`http://${iso('localhost')}:8080/`)
  })

  it('rejects a NON-loopback chat-fed URL (loopback-only channel)', () => {
    render(<WebPreviewPanel sessionKey="sess-1" />)
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
    render(<WebPreviewPanel sessionKey="sess-1" />)
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
      render(<WebPreviewPanel sessionKey="sess-1" />)
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

  it('constrains the iframe to a device size when a mobile preset is picked', () => {
    render(<WebPreviewPanel sessionKey="sess-1" />)
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
    render(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByLabelText('Preview size'))
    const preset = screen.getByText('iPhone SE').closest('button') as HTMLButtonElement
    expect(preset.getAttribute('type')).toBe('button')
  })

  it('dispatches a snip request when the crop button is clicked', () => {
    let fired = false
    const handler = () => { fired = true }
    window.addEventListener('kirocrew-web-preview-snip', handler)
    try {
      render(<WebPreviewPanel sessionKey="sess-1" />)
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
      render(<WebPreviewPanel sessionKey="sess-1" />)
      fireEvent.click(screen.getByLabelText('Expand preview'))
      expect(seen).toContain(true)
      fireEvent.click(screen.getByLabelText('Exit expanded preview'))
      expect(seen).toContain(false)
    } finally {
      window.removeEventListener('kirocrew-preview-focus', handler)
    }
  })
})
