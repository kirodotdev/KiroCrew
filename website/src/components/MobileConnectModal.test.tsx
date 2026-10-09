/**
 * MobileConnectModal — the sidebar "Connect your phone" dialog.
 *
 * Pins the credential-safety contract and the seam's forward-compat shape:
 *  1. a QR/link credential is minted ONLY on explicit click, never on mount
 *     (the responses carry live session tokens);
 *  2. sections render per `kinds` from the governed methods endpoint; a kind with
 *     neither a built-in section nor a registered renderer renders NOTHING (an
 *     unknown method degrades to absent, never to a broken panel), while an
 *     edition's kind draws through the renderer seam
 *     (`mobileConnectRenderers.tsx`);
 *  3. the not-ready tailnet state routes to the real setup card instead of
 *     minting.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { TOPBAR_FOCUS_Z } from '../lib/themeDecorLayer'

const mocks = vi.hoisted(() => ({
  tailnetMobile: vi.fn(),
  tailnetMobileQr: vi.fn(),
  mobileLoginLink: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))

import MobileConnectModal from './MobileConnectModal'
import {
  registerMobileConnectRenderer,
  BUILTIN_MOBILE_CONNECT_KINDS,
} from './mobileConnectRenderers'

function mount(kinds: string[], onClose: () => void = () => {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <MobileConnectModal kinds={kinds} onClose={onClose} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  mocks.tailnetMobile.mockReset()
  mocks.tailnetMobileQr.mockReset()
  mocks.mobileLoginLink.mockReset()
  mocks.tailnetMobile.mockResolvedValue({ step: 'ready' })
})

describe('MobileConnectModal', () => {
  it('never mints a credential on mount — QR appears only after the explicit click', async () => {
    mocks.tailnetMobileQr.mockResolvedValue({
      url: 'https://host/?token=live',
      image: 'data:image/png;base64,x',
    })
    mount(['tailnet_qr'])
    await waitFor(() => expect(screen.getByText('Show QR code')).toBeInTheDocument())
    expect(mocks.tailnetMobileQr).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('Show QR code'))
    await waitFor(() =>
      expect(screen.getByAltText('QR code for mobile access')).toBeInTheDocument(),
    )
    expect(mocks.tailnetMobileQr).toHaveBeenCalledTimes(1)
  })

  it('shows the QR at its natural size so the browser never blurs its modules', async () => {
    // A fixed 176px box squeezes a code of about 80 modules to roughly 2px a
    // module, with smoothing, which is too small and soft for a phone camera.
    mocks.tailnetMobileQr.mockResolvedValue({
      url: 'https://host/?token=live',
      image: 'data:image/png;base64,x',
    })
    mount(['tailnet_qr'])
    fireEvent.click(await screen.findByText('Show QR code'))
    const img = await screen.findByAltText('QR code for mobile access')
    expect(img).not.toHaveAttribute('width')
    expect(img).not.toHaveAttribute('height')
    expect(img.className).toContain('[image-rendering:pixelated]')
    expect(img.className).toContain('max-w-full')
  })

  it('not-ready tailnet routes to setup instead of offering a mint', async () => {
    mocks.tailnetMobile.mockResolvedValue({ step: 'publish' })
    mount(['tailnet_qr'])
    await waitFor(() =>
      expect(
        screen.getByText(/Remote access is not set up yet/),
      ).toBeInTheDocument(),
    )
    expect(screen.queryByText('Show QR code')).not.toBeInTheDocument()
  })

  it('login_link mints only on click and shows the one-time URL', async () => {
    mocks.mobileLoginLink.mockResolvedValue({ url: 'https://ext/?token=once', expires_in: 300 })
    mount(['login_link'])
    expect(mocks.mobileLoginLink).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Create sign-in link'))
    await waitFor(() =>
      expect(screen.getByDisplayValue('https://ext/?token=once')).toBeInTheDocument(),
    )
  })

  it('an unrecognised kind renders nothing (forward compat with edition methods)', () => {
    mount(['some-enterprise-kind'])
    // Header renders; neither known section's affordance does.
    expect(screen.getByText('Use Kiro Crew on your phone')).toBeInTheDocument()
    expect(screen.queryByText('Show QR code')).not.toBeInTheDocument()
    expect(screen.queryByText('Create sign-in link')).not.toBeInTheDocument()
    expect(mocks.tailnetMobile).not.toHaveBeenCalled()
  })

  it('a failed probe offers an in-place Try again that re-probes', async () => {
    mocks.tailnetMobile.mockRejectedValueOnce(new Error('down'))
    mount(['tailnet_qr'])
    const retry = await screen.findByText('Try again')
    mocks.tailnetMobile.mockResolvedValue({ step: 'ready' })
    fireEvent.click(retry)
    await screen.findByText('Show QR code')
    expect(mocks.tailnetMobile).toHaveBeenCalledTimes(2)
  })

  it('the not-ready guidance closes the dialog before navigating', async () => {
    mocks.tailnetMobile.mockResolvedValue({ step: 'publish' })
    const onClose = vi.fn()
    mount(['tailnet_qr'], onClose)
    fireEvent.click(await screen.findByText(/Remote access is not set up yet/))
    expect(onClose).toHaveBeenCalled()
  })

  it('a minted QR offers New code, which re-mints in place', async () => {
    mocks.tailnetMobileQr.mockResolvedValue({
      url: 'https://ts/?token=live', image: 'data:image/png;base64,x',
      ttl_secs: 3600, link_window_secs: 300,
    })
    mount(['tailnet_qr'])
    fireEvent.click(await screen.findByText('Show QR code'))
    fireEvent.click(await screen.findByText('New code'))
    await waitFor(() => expect(mocks.tailnetMobileQr).toHaveBeenCalledTimes(2))
  })

  it('a failed QR mint reports the error inline', async () => {
    mocks.tailnetMobileQr.mockRejectedValue(new Error('boom'))
    mount(['tailnet_qr'])
    fireEvent.click(await screen.findByText('Show QR code'))
    await screen.findByText(/Could not generate a code/)
  })

  it('a failed link mint reports the error inline', async () => {
    mocks.mobileLoginLink.mockRejectedValue(new Error('no external origin'))
    mount(['login_link'])
    fireEvent.click(screen.getByText('Create sign-in link'))
    await screen.findByText(/Could not create a link/)
  })

  it('tells a restricted session to switch sessions instead of retrying', async () => {
    mocks.mobileLoginLink.mockRejectedValue(
      Object.assign(new Error('restricted session'), {
        body: JSON.stringify({ code: 'restricted_session' }),
      }),
    )
    mount(['login_link'])
    fireEvent.click(screen.getByText('Create sign-in link'))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      'Incognito and temporary sessions cannot create sign-in links. Switch to persistent mode to create one.',
    )
    expect(alert).not.toHaveTextContent('Try again')
  })

  it('tells an expired session to sign in again instead of retrying', async () => {
    mocks.mobileLoginLink.mockRejectedValue(
      Object.assign(new Error('caller session expired'), {
        body: JSON.stringify({ code: 'caller_session_expired' }),
      }),
    )
    mount(['login_link'])
    fireEvent.click(screen.getByText('Create sign-in link'))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Your session has expired. Sign in again, then create the link.')
    expect(alert).not.toHaveTextContent('Try again')
  })

  it('Copy link confirms with a transient tick', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    })
    mocks.mobileLoginLink.mockResolvedValue({ url: 'https://ext/?token=once', expires_in: 300 })
    mount(['login_link'])
    fireEvent.click(screen.getByText('Create sign-in link'))
    fireEvent.click(await screen.findByText('Copy link'))
    // The tick swaps the icon; the label persists — assert the click didn't throw
    // and the value stayed rendered (the copy path completed).
    await waitFor(() => expect(screen.getByDisplayValue('https://ext/?token=once')).toBeInTheDocument())
  })
})

describe('MobileConnectModal — edition renderer seam', () => {
  // Registered once for the file: the registry is a module singleton read at
  // render (registration is a composition-time act, not per-test state), and
  // these kinds are unique to this block so no other test's `kinds` matches them.
  registerMobileConnectRenderer({
    kind: 'modal_test_tunnel_qr',
    component: () => <div>edition tunnel section</div>,
  })
  registerMobileConnectRenderer({
    kind: 'modal_test_throws',
    component: () => {
      throw new Error('renderer exploded')
    },
  })

  it('draws a registered renderer for a kind the deployment offers', () => {
    mount(['modal_test_tunnel_qr'])
    expect(screen.getByText('edition tunnel section')).toBeInTheDocument()
    // The edition owns its own mint endpoint, so no built-in mint is touched.
    expect(mocks.tailnetMobile).not.toHaveBeenCalled()
    expect(mocks.tailnetMobileQr).not.toHaveBeenCalled()
    expect(mocks.mobileLoginLink).not.toHaveBeenCalled()
  })

  it('draws nothing for a registered kind the deployment does NOT offer', () => {
    // The seam cannot widen governance: the endpoint filters every id through
    // `capabilities.mobile_connect` before a kind reaches this dialog, so a
    // renderer for a denied or absent method has no section to draw.
    mount(['login_link'])
    expect(screen.queryByText('edition tunnel section')).not.toBeInTheDocument()
  })

  it('renders the edition section above the built-in link, which keeps working', () => {
    mount(['modal_test_tunnel_qr', 'login_link'])
    const edition = screen.getByText('edition tunnel section')
    const builtin = screen.getByText('Create sign-in link')
    // DOCUMENT_POSITION_FOLLOWING: a contributed method is the deployment's
    // primary way in, and the built-in link is the fallback beneath it.
    expect(edition.compareDocumentPosition(builtin) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('a throwing renderer disables only itself', () => {
    // Each section has its own ErrorBoundary, so one bad edition renderer must
    // not blank the dialog and take the built-in sections down with it.
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      mount(['modal_test_throws', 'login_link'])
      expect(screen.getByText('Use Kiro Crew on your phone')).toBeInTheDocument()
      expect(screen.getByText('Create sign-in link')).toBeInTheDocument()
    } finally {
      err.mockRestore()
    }
  })

  it('a throwing renderer that is the ONLY method still leaves usable content', () => {
    // No `fallback={null}` here, unlike the Overview stat-card slot: a vanishing
    // card leaves a grid of siblings, but this section can be the whole dialog,
    // and emptying it would strand a user who arrived from a nav row that
    // promised a way in.
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      mount(['modal_test_throws'])
      expect(screen.getByText('Something went wrong')).toBeInTheDocument()
      expect(screen.getByText('Try Again')).toBeInTheDocument()
    } finally {
      err.mockRestore()
    }
  })
})

describe('MobileConnectModal — every built-in kind actually draws', () => {
  // Pins `BUILTIN_MOBILE_CONNECT_KINDS` to the sections that exist. A member
  // added to that constant without a section here would report as drawable,
  // show the nav row, and then open a dialog with an empty body — the exact
  // outcome the renderer seam exists to prevent.
  it.each(BUILTIN_MOBILE_CONNECT_KINDS)('%s renders a section', async kind => {
    mocks.tailnetMobileQr.mockResolvedValue({ url: 'https://h/?t=x', image: 'data:image/png;base64,x' })
    mocks.mobileLoginLink.mockResolvedValue({ url: 'https://ext/?token=once', expires_in: 300 })
    mount([kind])
    // Each built-in section's mint affordance is its proof of presence.
    const affordance = kind === 'tailnet_qr' ? 'Show QR code' : 'Create sign-in link'
    expect(await screen.findByText(affordance)).toBeInTheDocument()
  })
})

describe('MobileConnectModal — paints above the chat chrome, below the takeovers', () => {
  // The dialog opens from the sidebar while the chat page's sessions flyout can
  // be expanded. That flyout and its drawer morph sit above the chat pane
  // (`SessionFlyout.tsx`, z-[59]/z-[60]), the focus-peek rail toggle above them
  // (z-[61]) and the focus-mode rail above that (inline TOPBAR_FOCUS_Z), so a panel
  // on the chat-pane ceiling (z-50) paints UNDER them. The component therefore
  // splits into two sibling layers, the same split Modal.tsx uses: a z-50
  // BACKDROP, which the desktop nav rail — a DOM-later z-50 sibling in the
  // shell's stacking context — beats on the tie, keeping nav rows clickable
  // while the dialog is open; and a pointer-events-none DIALOG LAYER above all
  // of that chrome. The dialog layer must ALSO stay below the shell's z-[100]
  // full-screen takeovers (`UpdateModal.tsx`'s "Installing update…" surface and
  // friends): this component renders inside the App shell's `relative z-[1]`
  // root — NOT on document.body where Modal.tsx portals — so a z-[100] here
  // ties with the DOM-earlier takeovers and wins on document order, keeping a
  // live QR on screen over the very surface meant to hide everything. jsdom
  // does no painting, so this compares the layers the sources declare, which
  // is the property paint order follows.
  const readSource = (...parts: string[]) =>
    readFileSync(join(__dirname, '..', ...parts), 'utf8')
  const zLayers = (src: string) =>
    [...src.matchAll(/\bz-(?:\[(\d+)\]|(\d+))(?![\w-])/g)].map(m => Number(m[1] ?? m[2]))
  const dialogLayer = () => screen.getByRole('dialog').parentElement as HTMLElement
  const dialogLayerZ = () => {
    const layers = zLayers(dialogLayer().className)
    expect(layers).toHaveLength(1)
    return layers[0]
  }
  const backdropZ = () => {
    const layers = zLayers(screen.getByRole('presentation').className)
    expect(layers).toHaveLength(1)
    return layers[0]
  }

  it('the dialog layer sits above every chat-chrome layer', () => {
    mount(['login_link'])
    const appSrc = readSource('App.tsx')
    // The sessions flyout and its drawer morph.
    const flyout = zLayers(readSource('pages', 'chat', 'SessionFlyout.tsx'))
    expect(flyout.length).toBeGreaterThan(0)
    // The focus-peek layers (rail toggle included).
    const peek = appSrc
      .split('\n')
      .filter(line => line.includes('focus-peek-'))
      .flatMap(zLayers)
    expect(peek.length).toBeGreaterThan(0)
    // The focus-mode rail's INLINE zIndex — a style prop, invisible to the
    // z-[N] scan, so read it from the rail's own style block (a literal or the
    // shared TOPBAR_FOCUS_Z layer) and fail loudly if the block stops
    // declaring one.
    const railAt = appSrc.indexOf('focus-chrome-rail')
    expect(railAt).toBeGreaterThan(-1)
    const railInline = appSrc.slice(railAt, railAt + 2000).match(/zIndex:\s*(\d+|TOPBAR_FOCUS_Z)\b/)
    expect(railInline).not.toBeNull()
    const railZ = railInline![1] === 'TOPBAR_FOCUS_Z' ? TOPBAR_FOCUS_Z : Number(railInline![1])
    const chrome = [...flyout, ...peek, railZ]
    expect(dialogLayerZ()).toBeGreaterThan(Math.max(...chrome))
  })

  it('the dialog layer sits below the full-screen takeovers, which must hide a live QR', () => {
    mount(['login_link'])
    // UpdateModal's full-screen takeover (the "Installing update…" /
    // install-failed surface) — the overlay that must cover everything,
    // including an open panel. It is the HIGHEST `fixed inset-0 z-[N]` overlay
    // UpdateModal declares: since #15776 that component ALSO renders a
    // dismissible "update ready" dialog in the chat-chrome band (z-[68], the
    // same layer as this one), so the takeover is specifically the top overlay,
    // not every one. The dialog renders in the same shell stacking context
    // DOM-later, so a tie or more would paint the panel — and its live QR/link —
    // over the takeover and cover the install-failed card's "Back to dashboard"
    // button.
    const overlays = [
      ...readSource('components', 'UpdateModal.tsx')
        .matchAll(/fixed inset-0 z-\[(\d+)\]/g),
    ].map(m => Number(m[1]))
    expect(overlays.length).toBeGreaterThan(0)
    const takeover = Math.max(...overlays)
    expect(dialogLayerZ()).toBeLessThan(takeover)
  })

  it('clicks outside the panel pass through the dialog layer to what is underneath', () => {
    mount(['login_link'])
    // The layer swallows nothing (the document-level pointerdown handler sees
    // every outside click on the real target); only the panel takes pointers.
    expect(dialogLayer().className.split(/\s+/)).toContain('pointer-events-none')
    expect(screen.getByRole('dialog').className.split(/\s+/)).toContain('pointer-events-auto')
  })

  it('the backdrop never outranks the desktop nav rail, so nav rows stay clickable', () => {
    mount(['login_link'])
    // The rail is the DOM-later sibling, so it wins a z tie; a backdrop above
    // its z would hit-test every nav-row click, making a tab take two clicks
    // (one to dismiss, one to navigate). Read the rail's OWN className line —
    // and fail loudly if that line stops declaring a z.
    const railLines = readSource('App.tsx')
      .split('\n')
      .filter(line => line.includes('focus-chrome-rail') && line.includes('className='))
    expect(railLines).toHaveLength(1)
    const railZ = zLayers(railLines[0])
    expect(railZ.length).toBeGreaterThan(0)
    expect(backdropZ()).toBeLessThanOrEqual(Math.min(...railZ))
  })
})
