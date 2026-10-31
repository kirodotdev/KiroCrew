import type React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import WebPreviewPanel, { normalizeUrl, setSessionPreviewUrl, setSessionPreviewPending, isolatePreviewHost, isDashboardOrigin, withCacheBuster, httpEmbedRefusal, isPotentiallyTrustworthyHost } from '../components/WebPreviewPanel'

// The crop button is gated on snip support (getDisplayMedia). Force it on so
// the button renders under happy-dom (which has no mediaDevices.getDisplayMedia).
vi.mock('../hooks/useScreenSnip', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useScreenSnip')>()
  return { ...actual, isScreenSnipSupported: () => true }
})

/** The Preview tab's allowlist as the tests' gateway answers it: every loopback
 *  host spelling on the ports the cases below type, so a case about framing is
 *  not refused by the gate (which has its own cases). */
const PORTS = [80, 443, 1234, 3000, 3001, 4000, 4321, 5000, 5173, 5174, 8000, 8080, 8081, 9000, 9223]
const ORIGINS = PORTS.flatMap(p => [`localhost:${p}`, `127.0.0.1:${p}`, `[::1]:${p}`])
const sendErrorToChat = vi.fn(() => true)
vi.mock('../utils/errorReport', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/errorReport')>()),
  sendErrorToChat: (...args: unknown[]) => sendErrorToChat(...(args as [])),
}))

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

beforeEach(() => {
  kirocrewConfig.mockReset().mockResolvedValue({ dashboard: { browser_local_origins: ORIGINS } })
  patchConfig.mockReset().mockResolvedValue({ ok: true })
  sendErrorToChat.mockClear()
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
  it('adds an http scheme to a bare host:port', async () => {
    expect(normalizeUrl('localhost:5173')).toBe('http://localhost:5173/')
    expect(normalizeUrl('127.0.0.1:8080')).toBe('http://127.0.0.1:8080/')
  })
  it('upgrades a bare public host to https (what a real site answers on)', async () => {
    expect(normalizeUrl('google.com')).toBe('https://google.com/')
    expect(normalizeUrl('www.example.com/path?q=1')).toBe('https://www.example.com/path?q=1')
  })
  it('keeps http for the dev-server shapes: loopback, an IP literal, an explicit port', async () => {
    expect(normalizeUrl('localhost')).toBe('http://localhost/')
    expect(normalizeUrl('myapp.localhost:5173')).toBe('http://myapp.localhost:5173/')
    expect(normalizeUrl('192.168.1.4')).toBe('http://192.168.1.4/')
    expect(normalizeUrl('192.168.1.4:3000')).toBe('http://192.168.1.4:3000/')
    expect(normalizeUrl('example.com:8443')).toBe('http://example.com:8443/')
  })
  it('keeps explicit http/https', async () => {
    expect(normalizeUrl('https://example.com')).toBe('https://example.com/')
    expect(normalizeUrl('http://example.com')).toBe('http://example.com/')
  })
  it('rejects empty and non-http(s) schemes', async () => {
    expect(normalizeUrl('   ')).toBeNull()
    expect(normalizeUrl('javascript:alert(1)')).toBeNull()
    expect(normalizeUrl('file:///etc/passwd')).toBeNull()
  })
})

describe('isolatePreviewHost', () => {
  it('swaps a loopback preview host that equals the dashboard host to the other alias', async () => {
    expect(isolatePreviewHost('http://localhost:5173/', 'localhost')).toBe('http://127.0.0.1:5173/')
    expect(isolatePreviewHost('http://127.0.0.1:5173/', '127.0.0.1')).toBe('http://localhost:5173/')
  })
  it('isolates a same-host *.localhost dashboard (e.g. kirocrew.localhost) to 127.0.0.1', async () => {
    expect(isolatePreviewHost('http://kirocrew.localhost:5173/', 'kirocrew.localhost'))
      .toBe('http://127.0.0.1:5173/')
  })
  it('leaves a preview host that already differs from the dashboard host', async () => {
    expect(isolatePreviewHost('http://127.0.0.1:5173/', 'localhost')).toBe('http://127.0.0.1:5173/')
    expect(isolatePreviewHost('http://localhost:5173/', 'kirocrew.localhost')).toBe('http://localhost:5173/')
  })
  it('leaves non-loopback hosts untouched', async () => {
    expect(isolatePreviewHost('https://example.com/', 'localhost')).toBe('https://example.com/')
  })
  it('is a no-op when the dashboard host is unknown', async () => {
    expect(isolatePreviewHost('http://localhost:5173/', '')).toBe('http://localhost:5173/')
  })
  it('canonicalizes an IPv6 loopback ([::1]) preview host to 127.0.0.1 (CSP cannot admit [::1]:*)', async () => {
    // The dashboard CSP structurally cannot admit `http://[::1]:*`, so the
    // liveness probe to [::1] is refused and a healthy server shows unreachable.
    expect(isolatePreviewHost('http://[::1]:8765/', 'localhost')).toBe('http://127.0.0.1:8765/')
    expect(isolatePreviewHost('http://[::1]:8765/app?x=1#h', 'localhost'))
      .toBe('http://127.0.0.1:8765/app?x=1#h')
  })
  it('canonicalizes [::1] even when the dashboard host is unknown (CSP gap is host-independent)', async () => {
    expect(isolatePreviewHost('http://[::1]:8765/', '')).toBe('http://127.0.0.1:8765/')
  })
  it('canonicalizes [::1] then still cookie-isolates against a 127.0.0.1 dashboard', async () => {
    // [::1] → 127.0.0.1, which now equals the dashboard host → isolate to localhost.
    expect(isolatePreviewHost('http://[::1]:8765/', '127.0.0.1')).toBe('http://localhost:8765/')
  })
})


describe('isDashboardOrigin', () => {
  it('matches the gateway across the two loopback aliases (same port)', async () => {
    // The real case: the panel is on one alias, the target names the other, and
    // both are the same listening server — which refuses to be framed.
    expect(isDashboardOrigin('http://127.0.0.1:6776/api/hooks/agent', 'http://localhost:6776/')).toBe(true)
    expect(isDashboardOrigin('http://localhost:6776/', 'http://127.0.0.1:6776/chat')).toBe(true)
    expect(isDashboardOrigin('http://127.0.0.1:6776/', 'http://kirocrew.localhost:6776/')).toBe(true)
  })
  it('does not match a dev server on a different port', async () => {
    expect(isDashboardOrigin('http://localhost:5173/', 'http://localhost:6776/')).toBe(false)
    expect(isDashboardOrigin('http://127.0.0.1:3000/', 'http://localhost:6776/')).toBe(false)
  })
  it('treats an absent port as the scheme default, so :80 and bare compare equal', async () => {
    expect(isDashboardOrigin('http://localhost/', 'http://localhost:80/')).toBe(true)
    expect(isDashboardOrigin('http://localhost:80/', 'http://localhost/')).toBe(true)
    expect(isDashboardOrigin('http://localhost:8080/', 'http://localhost/')).toBe(false)
  })
  it('never matches a non-loopback target', async () => {
    expect(isDashboardOrigin('https://example.com/', 'https://example.com/')).toBe(false)
  })
  it('never matches when the dashboard itself is remote', async () => {
    // Over a tunnel or a LAN address, a loopback target is the USER's own
    // machine — an ordinary dev server — not this gateway.
    expect(isDashboardOrigin('http://localhost:443/', 'https://crew.example.com/')).toBe(false)
    expect(isDashboardOrigin('http://localhost:6776/', 'http://192.168.1.4:6776/')).toBe(false)
  })
  it('returns false for an unparseable url or an unknown dashboard origin', async () => {
    expect(isDashboardOrigin('not a url', 'http://localhost:6776/')).toBe(false)
    expect(isDashboardOrigin('http://localhost:6776/', '')).toBe(false)
  })
})

describe('withCacheBuster', () => {
  it('appends the reload counter for any non-initial load', async () => {
    expect(withCacheBuster('http://localhost:8080/', 1)).toBe('http://localhost:8080/?_kcreload=1')
    expect(withCacheBuster('http://localhost:8080/', 7)).toBe('http://localhost:8080/?_kcreload=7')
  })
  it('leaves the URL pristine for the initial load (key 0) and for an empty URL', async () => {
    expect(withCacheBuster('http://localhost:8080/', 0)).toBe('http://localhost:8080/')
    expect(withCacheBuster('', 3)).toBe('')
  })
  it('preserves an existing query and fragment, inserting the param before the hash', async () => {
    expect(withCacheBuster('http://localhost:5173/app?tab=logs#section-2', 2))
      .toBe('http://localhost:5173/app?tab=logs&_kcreload=2#section-2')
  })
  it('replaces its own param instead of stacking copies across reloads', async () => {
    const once = withCacheBuster('http://localhost:5173/', 1)
    expect(withCacheBuster(once, 2)).toBe('http://localhost:5173/?_kcreload=2')
  })
  it('returns the input unchanged when it cannot be parsed', async () => {
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

  it('shows the empty state with the allowed origins as quick picks before a URL is set', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    await screen.findByText('localhost:5173')
    expect(screen.queryByTitle('Web preview')).toBeNull()
    // Quick-pick buttons are type=button so a valid draft in the URL field
    // can't be overridden by a stray form submission.
    expect((screen.getByText('localhost:5173').closest('button') as HTMLButtonElement).getAttribute('type')).toBe('button')
  })

  it('loads a typed URL into the iframe (normalizing scheme + isolating host) on submit', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'localhost:8080' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:8080/`)
  })

  it('enables back only after navigating to a second URL, and steps back', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByLabelText('Back')).toBeDisabled()
    fireEvent.click(screen.getByText('localhost:3000'))
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

  it('loads a quick-pick port (isolated host)', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:3000/`)
  })

  it('persists the URL per session and restores it on mount', async () => {
    localStorage.setItem('mc-webpreview-url:sess-1', 'http://localhost:4321/')
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:4321/`)
    renderWithProviders(<WebPreviewPanel sessionKey="sess-2" />)
    await waitFor(() => expect(screen.getAllByTestId('web-preview-panel')[1].getAttribute('data-origins-loaded')).toBe('true'))
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('loads a URL fed externally via setSessionPreviewUrl (matching slot, isolated)', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    act(() => { setSessionPreviewUrl('sess-1', 'localhost:8080') })
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:8080/`)
  })

  it('does not live-load an external feed when open=false (offer only)', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewUrl('sess-1', 'localhost:8080', false) })
    // No dispatch → the already-mounted panel stays on the empty state.
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
  })

  it('ignores an external feed aimed at a different slot', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewUrl('sess-2', 'localhost:8080') })
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
  })

  it('shows a Load-preview card for a pending feed and navigates only on the explicit click', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewPending('sess-1', 'localhost:8080') })
    // Pending → a card is shown and the iframe is NOT loaded (no auto-GET).
    expect(screen.getByText('Preview ready')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    // Explicit click is what fires the load.
    fireEvent.click(screen.getByText('Load preview'))
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:8080/`)
  })

  it('rejects a NON-loopback chat-fed URL (loopback-only channel)', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
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

  it('reports a target that is this gateway instead of framing a blank page', async () => {
    // The dashboard's own origin can never render in the iframe: the gateway
    // sends frame-ancestors 'self' and the cookie-isolation host swap makes the
    // frame cross-origin by construction. The liveness probe can't see that
    // (the server IS up), so the panel has to say so itself.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: `${window.location.host}/api/hooks/agent` } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.getByText("Can't preview this dashboard here")).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    // The target is still named, and an escape hatch is offered.
    expect(screen.getByText(/\/api\/hooks\/agent/)).toBeInTheDocument()
    expect(screen.getByText('Open in browser')).toBeInTheDocument()
  })

  it('refuses a non-loopback http target as an outside address, before any embed question', async () => {
    const originalHref = window.location.href
    window.location.href = 'https://dashboard.example.com/'
    try {
      await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
      const input = screen.getByLabelText('Preview URL')
      fireEvent.change(input, { target: { value: 'http://0.0.0.0:5173/very/long/path' } })
      fireEvent.submit(input.closest('form') as HTMLFormElement)
      // 0.0.0.0 is not a loopback host, so no allowlist entry could ever admit
      // it: the outside-address card, with the real browser as the way out.
      expect(screen.getByTestId('web-preview-outside-refused')).toBeInTheDocument()
      expect(screen.queryByTitle('Web preview')).toBeNull()
      const shown = 'http://0.0.0.0:5173/very/long/path'
      expect(screen.getByText(shown).tagName).toBe('CODE')
      expect(screen.getByRole('link', { name: /Open in your Chrome/ })).toHaveAttribute('href', shown)
    } finally {
      window.location.href = originalHref
    }
  })

  // The refusal is about the TARGET's origin, not the dashboard's scheme alone.
  // Loopback is potentially trustworthy, so an https-served dashboard (a tunnel,
  // which is the deployment the CLI browser view's port pin exists for) has to
  // frame it rather than explain a block the engine never makes.
  it.each([
    ['http://127.0.0.1:9223/', 'http://127.0.0.1:9223/'],
    ['http://localhost:5173/', 'http://localhost:5173/'],
  ])('frames a loopback target on an https dashboard (%s)', async (typed, expected) => {
    const originalHref = window.location.href
    window.location.href = 'https://dashboard.example.com/'
    try {
      await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
      const input = screen.getByLabelText('Preview URL')
      fireEvent.change(input, { target: { value: typed } })
      fireEvent.submit(input.closest('form') as HTMLFormElement)

      expect(screen.queryByText("Can't embed an http:// page here")).toBeNull()
      const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
      expect(targetOf(frame)).toBe(expected)
    } finally {
      window.location.href = originalHref
    }
  })

  // `*.localhost` is potentially trustworthy too, but the dashboard's frame-src
  // only admits it in instances mode, so relaxing the guard for it would trade
  // the explanation for a CSP-blanked frame.
  it('refuses a *.localhost target as an outside address: the allowlist takes loopback spellings only', async () => {
    const originalHref = window.location.href
    window.location.href = 'https://dashboard.example.com/'
    try {
      await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
      const input = screen.getByLabelText('Preview URL')
      fireEvent.change(input, { target: { value: 'http://app.localhost:5173/' } })
      fireEvent.submit(input.closest('form') as HTMLFormElement)
      expect(screen.getByTestId('web-preview-outside-refused')).toBeInTheDocument()
      expect(screen.queryByTitle('Web preview')).toBeNull()
    } finally {
      window.location.href = originalHref
    }
  })

  describe('httpEmbedRefusal', () => {
    it('does not arise on an http dashboard or for an https target', async () => {
      expect(httpEmbedRefusal('http://myapp.localhost:5173/', 'http:')).toBeNull()
      expect(httpEmbedRefusal('http://0.0.0.0:5173/', 'http:')).toBeNull()
      expect(httpEmbedRefusal('https://myapp.localhost:5173/', 'https:')).toBeNull()
    })

    it('admits the hosts the dashboard frame-src always carries', async () => {
      expect(httpEmbedRefusal('http://127.0.0.1:5173/', 'https:')).toBeNull()
      expect(httpEmbedRefusal('http://localhost:5173/', 'https:')).toBeNull()
    })

    it.each([
      ['http://myapp.localhost:5173/'],
      ['http://127.0.0.2:5173/'],
      ['http://[::1]:5173/'],
    ])('names the dashboard policy for a trustworthy host with no frame-src entry (%s)', (url) => {
      expect(httpEmbedRefusal(url, 'https:')).toBe('policy')
    })

    it.each([
      ['http://0.0.0.0:5173/'],
      ['http://example.com/'],
      ['http://10.0.0.7:5173/'],
    ])('names the browser for a host the engine blocks as mixed content (%s)', (url) => {
      expect(httpEmbedRefusal(url, 'https:')).toBe('browser')
    })

    it('fails closed to the browser wording for an unparseable URL', async () => {
      expect(httpEmbedRefusal('http://exa mple/', 'https:')).toBe('browser')
    })
  })

  describe('isPotentiallyTrustworthyHost', () => {
    it('follows W3C Secure Contexts: loopback names and 127/8, not 0.0.0.0', async () => {
      for (const h of ['localhost', 'app.localhost', 'a.b.localhost', '127.0.0.1', '127.255.255.254', '[::1]', '::1']) {
        expect(isPotentiallyTrustworthyHost(h), h).toBe(true)
      }
      for (const h of ['0.0.0.0', 'example.com', 'localhost.example.com', '1270.0.0.1', '127.0.0', '', '10.0.0.1']) {
        expect(isPotentiallyTrustworthyHost(h), h).toBe(false)
      }
    })
  })

  // A public http:// host never reaches the frame on this transport at all: it is
  // handed to the gateway host's browser. Pinned so the relaxed guard is not read
  // as having opened the iframe to non-loopback plaintext.
  it('answers an outside URL with "open it in your Chrome, or ask Kiro" instead of framing it', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'http://example.com/' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    await screen.findByTestId('web-preview-outside-refused')
    expect(screen.queryByTitle('Web preview')).toBeNull()
    expect(screen.getByText('This is not a local address')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Open in your Chrome/ }).getAttribute('href')).toBe('http://example.com/')
    expect(screen.getByTestId('web-preview-ask-kiro')).toBeInTheDocument()
    // Nothing went to the gateway: no launcher, no allow.
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('offers a one-click Allow for a loopback address outside the list, and writes the whole list back', async () => {
    kirocrewConfig.mockResolvedValue({ dashboard: { browser_local_origins: ['localhost:5173'] } })
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'http://127.0.0.1:4200/app' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.getByTestId('web-preview-origin-refused')).toBeInTheDocument()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    expect(screen.queryByTestId('web-preview-outside-refused')).toBeNull()
    // The write lands on the gateway; the re-read after it carries the new entry.
    kirocrewConfig.mockResolvedValue({ dashboard: { browser_local_origins: ['localhost:5173', '127.0.0.1:4200'] } })
    fireEvent.click(screen.getByTestId('web-preview-allow-origin'))
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('dashboard.browser_local_origins', ['localhost:5173', '127.0.0.1:4200']))
    await waitFor(() => expect(screen.getByTitle('Web preview')).toBeInTheDocument())
  })

  it('frames the exact origin the user listed, through the cookie-isolation host swap', async () => {
    // The dashboard is served from `localhost`; the user lists `localhost:5173`
    // and loads that URL. The frame must load the ISOLATED host (127.0.0.1) so
    // the dashboard cookie never reaches the dev server, and the allowlist must
    // be judged on the host the user chose, not the rewritten one: judging the
    // rewrite refused the one origin they configured and offered to add a second.
    const typedHost = window.location.hostname === '127.0.0.1' ? '127.0.0.1' : 'localhost'
    kirocrewConfig.mockResolvedValue({ dashboard: { browser_local_origins: [`${typedHost}:5173`] } })
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: `http://${typedHost}:5173/` } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.queryByTestId('web-preview-origin-refused')).toBeNull()
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso(typedHost)}:5173/`)
    // The user-facing spellings keep what they typed.
    expect((input as HTMLInputElement).value).toBe(`http://${typedHost}:5173/`)
    expect(localStorage.getItem('mc-webpreview-url:sess-1')).toBe(`http://${typedHost}:5173/`)
  })

  it('with no origins configured, the empty state points at Settings instead of offering picks', async () => {
    kirocrewConfig.mockResolvedValue({ dashboard: { browser_local_origins: [] } })
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    expect(screen.getByTestId('web-preview-no-origins')).toBeInTheDocument()
    expect(screen.queryByTestId('web-preview-origin-picks')).toBeNull()
  })

  it('Ask Kiro stages a prompt naming the outside address', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'https://news.example.org/' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    fireEvent.click(screen.getByTestId('web-preview-ask-kiro'))
    expect(sendErrorToChat).toHaveBeenCalledTimes(1)
    expect(String(sendErrorToChat.mock.calls[0][0])).toContain('https://news.example.org/')
  })

  // The http-served dashboard (the default install) never had this refusal and
  // must not gain one.
  it('frames a loopback target on an http dashboard', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'http://127.0.0.1:9223/' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)

    expect(screen.queryByText("Can't embed an http:// page here")).toBeNull()
    expect(targetOf(screen.getByTitle('Web preview') as HTMLIFrameElement))
      .toBe(`http://${iso('127.0.0.1')}:9223/`)
  })

  it('still frames an ordinary dev server on another port', async () => {
    // Guards the port comparison: only the gateway's own port is refused.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'localhost:5173' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.queryByText("Can't preview this dashboard here")).toBeNull()
    expect(screen.getByTitle('Web preview')).toBeInTheDocument()
  })

  it('shows a self-origin target exactly as entered, not host-swapped', async () => {
    // The cookie-isolation swap protects a FRAMED server. This target is never
    // framed, so swapping it would tell someone who typed the dashboard's own
    // host that the OTHER loopback alias "is the dashboard's own server" while
    // the panel is explaining itself. Typing the dashboard's host is what
    // triggers the swap, so that is the case this pins.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
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

  it('still cookie-isolates an ordinary dev server it WILL frame', async () => {
    // Guards the narrowness of the exemption: only the never-framed self target
    // keeps its host.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'localhost:5173' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(targetOf(frame)).toBe(`http://${iso('localhost')}:5173/`)
  })

  it('refuses a chat-fed URL that points back at this gateway', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    // An agent discussing webhooks mentions the gateway's own URL in prose; the
    // offer must not be raised, since Load could only lead to a blank panel.
    let ret: string | null = 'sentinel'
    act(() => { ret = setSessionPreviewPending('sess-1', `http://${window.location.host}/api/hooks/agent`) })
    expect(ret).toBeNull()
    expect(screen.queryByText('Preview ready')).toBeNull()
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('dismisses a pending feed without navigating', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    act(() => { setSessionPreviewPending('sess-1', 'localhost:8080') })
    fireEvent.click(screen.getByText('Dismiss'))
    expect(screen.queryByText('Preview ready')).toBeNull()
    expect(screen.queryByTitle('Web preview')).toBeNull()
    expect(screen.getByText('Preview a local web server')).toBeInTheDocument()
  })

  it('shows a "not reachable" state after the dev server stops responding, then auto-restores', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('refused'))
    vi.stubGlobal('fetch', fetchMock)
    // The origins read settles on real timers; the probe cadence runs on fake ones.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    vi.useFakeTimers()
    try {
      fireEvent.click(screen.getByText('localhost:3000'))
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


  it('varies the iframe src on Reload so the remount is a new request, not a cache hit', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
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

  it('keeps the URL pristine on the initial mount-restored load (no stray param)', async () => {
    localStorage.setItem('mc-webpreview-url:sess-1', 'http://localhost:4321/')
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.src).toBe(`http://${iso('localhost')}:4321/`)
    expect(frame.src).not.toContain('_kcreload')
  })

  it('leaves the URL bar and the open-in-browser link on the clean URL', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    fireEvent.click(screen.getByLabelText('Reload preview'))
    // The user picked `localhost:3000`: that is what the bar shows and the link
    // opens. The cache-buster AND the cookie-isolation host swap are details of the
    // frame load: neither leaks into what the user sees, copies, or opens externally.
    const clean = 'http://localhost:3000/'
    expect((screen.getByLabelText('Preview URL') as HTMLInputElement).value).toBe(clean)
    expect((screen.getByLabelText('Open in browser') as HTMLAnchorElement).href).toBe(clean)
    expect(targetOf(screen.getByTitle('Web preview') as HTMLIFrameElement)).toBe(`http://${iso('localhost')}:3000/`)
  })

  it('probes liveness against the clean URL, not the cache-busted one', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    fireEvent.click(screen.getByLabelText('Reload preview'))
    const probed = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map(c => c[0])
    expect(probed.length).toBeGreaterThan(0)
    for (const u of probed) expect(String(u)).not.toContain('_kcreload')
  })

  it('constrains the iframe to a device size when a mobile preset is picked', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    let frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.style.width).toBe('')
    // Device presets now live inside the overflow menu → Preview size submenu.
    // Radix opens on pointerDown, not click.
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Preview size' }),
      { pointerId: 1, button: 0, ctrlKey: false, isPrimary: true },
    )
    fireEvent.click(screen.getByText('iPhone SE'))
    frame = screen.getByTitle('Web preview') as HTMLIFrameElement
    expect(frame.style.width).toBe('375px')
    expect(frame.style.height).toBe('667px')
  })

  it('device preset items carry menuitemradio semantics with aria-checked', async () => {
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    fireEvent.click(screen.getByText('localhost:3000'))
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Preview size' }),
      { pointerId: 1, button: 0, ctrlKey: false, isPrimary: true },
    )
    const responsive = screen.getByRole('menuitemradio', { name: /Responsive/ })
    expect(responsive).toHaveAttribute('aria-checked', 'true')
    const iphone = screen.getByRole('menuitemradio', { name: /iPhone SE/ })
    expect(iphone).toHaveAttribute('aria-checked', 'false')
  })

  it('action row renders exactly 4 top-level sibling controls (regression guard for max-two-buttons-per-row)', async () => {
    // The row is: back, forward, (URL container), expand, divider, preview-size, crop.
    // The URL container is its own group (not counted); the divider is decorative.
    // Sibling action controls = back + forward + expand + preview-size + crop = 5.
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const form = screen.getByLabelText('Preview URL').closest('form') as HTMLFormElement
    // Count direct-child buttons that are action controls (exclude the
    // URL container's internal reload/open-in-browser and the decorative divider).
    const directButtons = Array.from(form.children).filter(el => {
      if (el.getAttribute('aria-hidden') === 'true') return false // divider
      if (el.tagName === 'DIV') return false // URL container
      return el.tagName === 'BUTTON' || el.getAttribute('role') === 'button'
    })
    // 5 = back, forward, expand, preview-size trigger, crop (canSnip is mocked on).
    expect(directButtons).toHaveLength(5)
  })

  it('dispatches a snip request when the crop button is clicked', async () => {
    let fired = false
    const handler = () => { fired = true }
    window.addEventListener('kirocrew-web-preview-snip', handler)
    try {
      await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
      fireEvent.click(screen.getByLabelText('Screenshot an area into the chat'))
      expect(fired).toBe(true)
    } finally {
      window.removeEventListener('kirocrew-web-preview-snip', handler)
    }
  })

  it('broadcasts preview-expand true/false as the expand button toggles', async () => {
    const seen: boolean[] = []
    const handler = (e: Event) => seen.push(!!(e as CustomEvent<{ expanded?: boolean }>).detail?.expanded)
    window.addEventListener('kirocrew-preview-expand', handler)
    try {
      await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
      fireEvent.click(screen.getByLabelText('Expand preview'))
      expect(seen).toContain(true)
      fireEvent.click(screen.getByLabelText('Collapse'))
      expect(seen).toContain(false)
    } finally {
      window.removeEventListener('kirocrew-preview-expand', handler)
    }
  })
})

describe('WebPreviewPanel — native browser transport (desktop app)', () => {
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


  it('an ALLOWED address opens in the native view; the iframe path stays for the rest', async () => {
    const api = installNativeBridge(false)
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'http://localhost:5173/' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    await waitFor(() => expect(api.open).toHaveBeenCalledWith('sess-1', `http://${iso('localhost')}:5173/`))
  })

  it('never hands an unlisted or outside address to the native view', async () => {
    const api = installNativeBridge(false)
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    const input = screen.getByLabelText('Preview URL')
    fireEvent.change(input, { target: { value: 'https://example.com/' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.getByTestId('web-preview-outside-refused')).toBeInTheDocument()
    fireEvent.change(input, { target: { value: 'http://localhost:4200/' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(screen.getByTestId('web-preview-origin-refused')).toBeInTheDocument()
    expect(api.open).not.toHaveBeenCalled()
  })

  it('the native address bar navigates only within the list', async () => {
    const api = installNativeBridge(true, 'http://localhost:5173/')
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" />)
    // Two bars carry the label: the iframe path's (aria-hidden behind the surface) and the surface's own.
    const bar = await waitFor(() => {
      const bars = screen.getAllByLabelText('Preview URL')
      expect(bars.length).toBe(2)
      return bars[1]
    })
    fireEvent.change(bar, { target: { value: 'https://example.com/' } })
    fireEvent.submit(bar.closest('form') as HTMLFormElement)
    expect(api.navigate).not.toHaveBeenCalled()
    fireEvent.change(bar, { target: { value: 'http://127.0.0.1:3000/app' } })
    fireEvent.submit(bar.closest('form') as HTMLFormElement)
    await waitFor(() => expect(api.navigate).toHaveBeenCalledWith('sess-1', 'http://127.0.0.1:3000/app'))
  })

  it('hides (never destroys) the native view when the panel goes inactive, and closes it on unmount', async () => {
    const api = installNativeBridge(true)
    const { rerender, unmount } = await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    const { unmount } = await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
    expect(await screen.findByTestId('browser-annotate')).toHaveTextContent('Annotate')
    unmount()
    installNativeBridge(true) // older shell: no `annotate`
    await renderPanel(<WebPreviewPanel sessionKey="sess-2" active />)
    await waitFor(() => expect(screen.queryByTitle('Live browser session')).toBeNull())
    expect(screen.queryByTestId('browser-annotate')).toBeNull()
  })

  it('clicking Annotate starts pick mode (badge hint localized, numbering from 0), flips to Done, and Done stops', async () => {
    const { annotate } = installAnnotateBridge()
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
      await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
      await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    const { rerender, unmount } = await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    const { unmount } = await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    unmount()
    installAnnotateBridge([TARGET])
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
    await waitFor(() => expect(screen.getByTestId('browser-annotations')).toHaveTextContent('too far right'))
  })

  it('counts what is listed (picks, noted or not) -- the same rows Clear would wipe; only noted picks are sent', async () => {
    const { state } = installAnnotateBridge()
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    const { rerender } = await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
      await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
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
    await renderPanel(<WebPreviewPanel sessionKey="sess-1" active />)
    fireEvent.click(await screen.findByTestId('browser-annotate'))
    await screen.findByTestId('browser-annotations')
    await pickAndNote(state, 'too far right')
    fireEvent.click(screen.getByTestId('browser-annotations-send'))
    expect(await screen.findByTestId('browser-annotations-added')).toHaveTextContent(/nothing is sent until you send the message/)
  })
})

