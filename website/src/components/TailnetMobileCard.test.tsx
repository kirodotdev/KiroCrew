/**
 * Phone-access card (Settings → Overview).
 *
 * The card is a pure renderer of a SERVER-DERIVED `step` — `_derive_step` is the
 * single owner of that decision — so these tests pin each step's rendering
 * independently rather than trying to reproduce the derivation. Three properties
 * carry most of the weight and each has cost a real defect:
 *
 *  - **An unrecognised `step` renders nothing.** `step` is typed, which is a claim
 *    about the contract and not a fact about the bytes. Indexing the literal icon
 *    map with an unknown value yields `undefined`, and rendering `undefined` as a
 *    component throws — React escalates that to the nearest error boundary, so an
 *    unknown step in this OPTIONAL card blanks the whole Overview page.
 *  - **The QR is never minted on render.** Its payload is a live session token, so
 *    a render that fetched it would put a credential on screen (and in the query
 *    cache) unasked.
 *  - **The copy tick means the clipboard was actually written.** `copyToClipboard`
 *    is awaited; a tick shown on rejection is a lie told over an empty clipboard,
 *    and it lands on the one string this feature exists to hand to a phone.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { Component, type ReactNode } from 'react'
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import { renderWithProviders } from '../test/helpers'
import type { TailnetMobileData, TailnetMobileQr, TailnetMobileStep } from '../api/client'

/** Error boundary that RECORDS instead of re-rendering.
 *
 *  Stands in for the boundary the real Settings page has. It exists so a test can
 *  distinguish "the card chose to render nothing" from "the card threw and React
 *  unmounted the tree" — those produce an identical empty container, which is
 *  what made the first version of the unknown-step test pass against the very bug
 *  it was written for. */
class RenderProbe extends Component<
  { children: ReactNode; onError: (e: Error) => void },
  { failed: boolean }
> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  componentDidCatch(error: Error) {
    this.props.onError(error)
  }
  render() {
    return this.state.failed ? null : this.props.children
  }
}

vi.mock('../api/client', () => ({
  api: {
    tailnetMobile: vi.fn(),
    restartGateway: vi.fn(),
    tailnetMobilePublish: vi.fn(),
    tailnetMobileUnpublish: vi.fn(),
    tailnetMobileQr: vi.fn(),
    patchConfig: vi.fn(),
  },
}))

// Mocked deliberately rather than left to jsdom. The real helper falls back to a
// textarea + `document.execCommand`, which jsdom does not implement, so an
// unmocked call REJECTS — the tick assertions would then pass or fail for a
// reason that has nothing to do with the component.
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { TailnetMobileCard } from './TailnetMobileCard'

const mockApi = api as unknown as Record<string, ReturnType<typeof vi.fn>>
const mockCopy = copyToClipboard as unknown as ReturnType<typeof vi.fn>

const HOST = 'desk.tail1a2b3c.ts.net'
const ORIGIN = `https://${HOST}`
const LS_KEY = 'mc-tailnet-mobile-invite-expanded'

/** A `ready` machine — every field truthful for that step. Overrides shift it. */
function data(overrides: Partial<TailnetMobileData> = {}): TailnetMobileData {
  return {
    step: 'ready',
    host: HOST,
    origin: ORIGIN,
    installed: true,
    reachable: true,
    logged_in: true,
    peer_count: 2,
    peers_online: 1,
    trusted: true,
    startup_trusted: true,
    published: true,
    keep_awake: true,
    governance_pinned: false,
    detail: '',
    download_url: 'https://tailscale.com/download',
    ...overrides,
  } as TailnetMobileData
}

function qrPayload(overrides: Partial<TailnetMobileQr> = {}): TailnetMobileQr {
  return {
    url: `${ORIGIN}/?token=zzz-not-a-real-token`,
    image: 'data:image/png;base64,zzzQRIMAGE',
    ttl_secs: 3600,
    link_window_secs: 300,
    host: HOST,
    ...overrides,
  }
}

/** Mount with the status query pre-stubbed, and wait for the first paint. */
async function mount(d: TailnetMobileData | null = data()) {
  mockApi.tailnetMobile.mockResolvedValue(d)
  const r = renderWithProviders(<TailnetMobileCard />)
  if (d) await screen.findByText(/Phone access|Use this on your phone/)
  return r
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  mockCopy.mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  // Unconditional, so a spy installed by a test that FAILS mid-way cannot leak
  // into the rest of the file — a leaked localStorage spy reads as 30 unrelated
  // failures and hides the one real one.
  vi.restoreAllMocks()
})

describe('TailnetMobileCard — render gating', () => {
  it('renders nothing while the status query is still loading', () => {
    mockApi.tailnetMobile.mockReturnValue(new Promise(() => {}))
    const { container } = renderWithProviders(<TailnetMobileCard />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for a step this build does not recognise', async () => {
    // The defect this pins: an unknown step used to index the icon map to
    // `undefined`, and rendering that as a component THROWS — React escalates it
    // to the nearest error boundary, so an unknown step in this optional card
    // blanked the entire Settings Overview page.
    //
    // Two things make this test non-vacuous, and both were needed:
    //
    //  - It waits for the query to actually RESOLVE, not merely to be issued.
    //    Asserting straight after the request leaves the assertion running
    //    against the still-loading first paint, which renders null for a wholly
    //    unrelated reason and so passes with the guard removed.
    //  - It asserts through an error boundary. "The container is empty" is true
    //    of a crashed render too — React unmounts the tree — so an emptiness
    //    check alone cannot tell the guard working from the guard missing.
    mockApi.tailnetMobile.mockResolvedValue(
      data({ step: 'zzz_from_a_newer_gateway' as TailnetMobileStep }),
    )
    const caught: Error[] = []
    const { container, queryClient } = renderWithProviders(
      <RenderProbe onError={(e) => caught.push(e)}>
        <TailnetMobileCard />
      </RenderProbe>,
    )

    await waitFor(() =>
      expect(queryClient.getQueryState(['tailnet-mobile'])?.data).toBeTruthy(),
    )

    expect(caught).toEqual([])
    expect(screen.queryByText('Phone access')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
  })
})

describe('TailnetMobileCard — install invitation', () => {
  it('starts as a one-line teaser rather than a full card', async () => {
    await mount(data({ step: 'install', installed: false }))
    expect(screen.getByText('Use this on your phone')).toBeInTheDocument()
    expect(screen.getByText('needs Tailscale')).toBeInTheDocument()
    // The full card's copy must NOT be present yet — that is the whole point of
    // the teaser: no permanent panel advertising a product the user may not want.
    expect(screen.queryByText('Download Tailscale')).toBeNull()
  })

  it('expands on click, persists the choice, and re-probes the daemon', async () => {
    await mount(data({ step: 'install', installed: false }))
    expect(mockApi.tailnetMobile).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Use this on your phone' }))

    expect(await screen.findByText('Download Tailscale')).toBeInTheDocument()
    expect(window.localStorage.getItem(LS_KEY)).toBe('1')
    // Expanding refetches: this is what lets `install` drop its Re-check button
    // without making someone who just installed Tailscale wait out the 30s poll.
    await waitFor(() => expect(mockApi.tailnetMobile).toHaveBeenCalledTimes(2))
  })

  it('opens expanded when the stored preference says so', async () => {
    window.localStorage.setItem(LS_KEY, '1')
    await mount(data({ step: 'install', installed: false }))
    expect(screen.getByText('Download Tailscale')).toBeInTheDocument()
  })

  it('collapses again from Hide, so expanding is not a one-way door', async () => {
    window.localStorage.setItem(LS_KEY, '1')
    await mount(data({ step: 'install', installed: false }))

    fireEvent.click(screen.getByRole('button', { name: 'Hide' }))

    expect(await screen.findByText('Use this on your phone')).toBeInTheDocument()
    expect(window.localStorage.getItem(LS_KEY)).toBe('0')
  })

  it('degrades to collapsed when localStorage throws', async () => {
    // Safari private browsing THROWS on access rather than returning null. An
    // unreadable preference must not take the card down with it.
    //
    // Scoped to THIS key rather than blanket-throwing: ThemeProvider (in the
    // render harness) reads localStorage too, so a blanket spy takes down the
    // wrapper instead of exercising the card, and the failure then looks like a
    // defect in every later test rather than in this stub.
    const realGet = window.localStorage.getItem.bind(window.localStorage)
    const realSet = window.localStorage.setItem.bind(window.localStorage)
    vi.spyOn(window.localStorage, 'getItem').mockImplementation((k: string) => {
      if (k === LS_KEY) throw new Error('zzz storage unavailable')
      return realGet(k)
    })
    vi.spyOn(window.localStorage, 'setItem').mockImplementation((k: string, v: string) => {
      if (k === LS_KEY) throw new Error('zzz storage unavailable')
      realSet(k, v)
    })

    await mount(data({ step: 'install', installed: false }))
    expect(screen.getByText('Use this on your phone')).toBeInTheDocument()

    // And a click whose choice cannot be persisted still expands, rather than
    // failing the interaction over an unwritable preference.
    fireEvent.click(screen.getByRole('button', { name: 'Use this on your phone' }))
    expect(await screen.findByText('Download Tailscale')).toBeInTheDocument()
  })

  it('offers exactly two buttons in the expanded row', async () => {
    window.localStorage.setItem(LS_KEY, '1')
    await mount(data({ step: 'install', installed: false }))
    // `max-two-buttons-per-row` (website/AUTOSDE.yaml, blocking) counts a link
    // styled as a button, and rejects wrapping as a fix — so Re-check is absent
    // here by design, not by omission.
    expect(screen.getByRole('button', { name: /Download Tailscale/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Re-check/ })).toBeNull()
  })
})

describe('TailnetMobileCard — step badges', () => {
  const cases: Array<[TailnetMobileStep, string]> = [
    ['ready', 'Active'],
    ['occupied', 'Needs attention'],
    ['pinned', 'Needs attention'],
    ['publish', 'Setup needed'],
    ['sign_in', 'Setup needed'],
    ['enable_https', 'Setup needed'],
  ]
  for (const [step, badge] of cases) {
    it(`shows "${badge}" for ${step}`, async () => {
      await mount(data({ step }))
      expect(screen.getByText(badge)).toBeInTheDocument()
    })
  }
})

describe('TailnetMobileCard — the daemon detail line', () => {
  it("shows the daemon's verbatim text where it is about the step", async () => {
    await mount(data({ step: 'sign_in', detail: 'zzz logged out (Tailscale needs login)' }))
    expect(
      screen.getByText('zzz logged out (Tailscale needs login)'),
    ).toBeInTheDocument()
  })

  it('withholds it on steps whose remedy is unrelated to it', async () => {
    // `detail` carries whichever of the serve state or the daemon probe spoke
    // last, so on `trust_off` — whose remedy is a config switch — it would render
    // a line about port occupancy next to an unrelated instruction, reading as
    // though the two were connected.
    await mount(data({ step: 'trust_off', detail: 'zzz port 443 already in use' }))
    expect(screen.queryByText('zzz port 443 already in use')).toBeNull()
    expect(screen.getByText('Set up & show QR')).toBeInTheDocument()
  })
})

describe('TailnetMobileCard — occupied', () => {
  it('renders the manual command its copy tells the operator to run', async () => {
    // Without this the body ("Publish it yourself if you are sure it is safe to
    // overwrite") is an instruction with no means to follow it.
    await mount(data({ step: 'occupied', published: null }))
    expect(screen.getByText('kirocrew tailnet up')).toBeInTheDocument()
    // And no publish button: publishing here would replace whatever Tailscale is
    // already serving, which is exactly what this step exists to avoid.
    expect(screen.queryByRole('button', { name: /Set up & show QR/ })).toBeNull()
  })
})

describe('TailnetMobileCard — ready', () => {
  it('shows the address, the sleep note, and both actions', async () => {
    await mount()
    expect(screen.getByText(ORIGIN)).toBeInTheDocument()
    expect(screen.getByText(/will not sleep while phone access is on/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Show QR code/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeInTheDocument()
    // Re-check belongs to the mid-setup steps; `ready` is not one.
    expect(screen.queryByRole('button', { name: /Re-check/ })).toBeNull()
  })

  it('omits the sleep note when keep_awake is off', async () => {
    await mount(data({ keep_awake: false }))
    expect(screen.queryByText(/will not sleep while phone access is on/)).toBeNull()
  })

  it('does NOT mint a QR on render', async () => {
    await mount()
    // The payload is a live session token. A render that fetched it would put a
    // credential on screen, and in the query cache, without being asked.
    expect(mockApi.tailnetMobileQr).not.toHaveBeenCalled()
    expect(screen.queryByAltText(/QR code/)).toBeNull()
  })

  it('warns when no other device has joined the tailnet', async () => {
    // Publishing and the QR both SUCCEED on a tailnet of one, so without this the
    // failure surfaces only as an unexplained "cannot connect" on the phone.
    await mount(data({ peer_count: 0, peers_online: 0 }))
    expect(screen.getByText(/No other device has joined/)).toBeInTheDocument()
  })

  it('hints more softly when peers exist but are all offline', async () => {
    await mount(data({ peer_count: 3, peers_online: 0 }))
    expect(screen.getByText(/none is online right now/)).toBeInTheDocument()
    expect(screen.queryByText(/No other device has joined/)).toBeNull()
  })

  it('shows neither warning once a peer is online', async () => {
    await mount(data({ peer_count: 3, peers_online: 2 }))
    expect(screen.queryByText(/No other device has joined/)).toBeNull()
    expect(screen.queryByText(/none is online right now/)).toBeNull()
  })
})

describe('TailnetMobileCard — the QR', () => {
  it('mints on request, renders the code, and states both time limits', async () => {
    mockApi.tailnetMobileQr.mockResolvedValue(qrPayload())
    await mount()

    fireEvent.click(screen.getByRole('button', { name: /Show QR code/ }))

    const img = await screen.findByAltText('QR code linking to this dashboard')
    expect(img).toHaveAttribute('src', 'data:image/png;base64,zzzQRIMAGE')
    // 300s → 5 min, 3600s → 1h. The link window is the part that surprises
    // people, so both numbers are rendered, not just the session TTL.
    expect(screen.getByText(/Scan within 5 min/)).toBeInTheDocument()
    expect(screen.getByText(/open your dashboard for 1h/)).toBeInTheDocument()
  })

  it('drops the code from state when dismissed', async () => {
    mockApi.tailnetMobileQr.mockResolvedValue(qrPayload())
    await mount()
    fireEvent.click(screen.getByRole('button', { name: /Show QR code/ }))
    await screen.findByAltText('QR code linking to this dashboard')

    fireEvent.click(screen.getByRole('button', { name: 'Hide' }))

    await waitFor(() =>
      expect(screen.queryByAltText('QR code linking to this dashboard')).toBeNull(),
    )
  })

  it('discards the code when phone access is turned off', async () => {
    mockApi.tailnetMobileQr.mockResolvedValue(qrPayload())
    mockApi.tailnetMobileUnpublish.mockResolvedValue({ ok: true, code: 'ok', detail: '' })
    await mount()
    fireEvent.click(screen.getByRole('button', { name: /Show QR code/ }))
    await screen.findByAltText('QR code linking to this dashboard')

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))

    // A live credential must not outlive the thing it grants access to.
    await waitFor(() =>
      expect(screen.queryByAltText('QR code linking to this dashboard')).toBeNull(),
    )
  })

  it('surfaces a mint failure instead of failing silently', async () => {
    mockApi.tailnetMobileQr.mockRejectedValue(new Error('zzz mint refused: not owner'))
    await mount()

    fireEvent.click(screen.getByRole('button', { name: /Show QR code/ }))

    expect(await screen.findByText('zzz mint refused: not owner')).toBeInTheDocument()
  })
})

describe('TailnetMobileCard — copy', () => {
  it('ticks only after the clipboard write actually resolves', async () => {
    await mount()
    const btn = screen.getByRole('button', { name: 'Copy' })
    expect(btn.querySelector('.lucide-check')).toBeNull()

    fireEvent.click(btn)

    await waitFor(() => expect(mockCopy).toHaveBeenCalledWith(ORIGIN))
    await waitFor(() => expect(btn.querySelector('.lucide-check')).not.toBeNull())
  })

  it('does not tick when the clipboard write rejects', async () => {
    // The defect this pins: the previous form called
    // `navigator.clipboard?.writeText(...)` and set the tick unconditionally, so
    // on a non-secure origin — where the optional chain short-circuits and
    // nothing is written — the user was shown a success tick over an empty
    // clipboard, on the one string this feature exists to hand to a phone.
    //
    // Asserted on the ICON, not the accessible name: `aria-label` is the static
    // `label` prop either way, so a name-based assertion holds whether the tick
    // appeared or not and cannot fail against the bug.
    mockCopy.mockRejectedValue(new Error('zzz clipboard unavailable'))
    await mount()
    const btn = screen.getByRole('button', { name: 'Copy' })

    fireEvent.click(btn)

    await waitFor(() => expect(mockCopy).toHaveBeenCalled())
    expect(btn.querySelector('.lucide-check')).toBeNull()
    expect(btn.querySelector('.lucide-copy')).not.toBeNull()
  })

  it('copies the QR link from the code panel', async () => {
    const payload = qrPayload()
    mockApi.tailnetMobileQr.mockResolvedValue(payload)
    await mount()
    fireEvent.click(screen.getByRole('button', { name: /Show QR code/ }))
    await screen.findByAltText('QR code linking to this dashboard')

    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))

    await waitFor(() => expect(mockCopy).toHaveBeenCalledWith(payload.url))
  })
})

describe('TailnetMobileCard — mutating actions', () => {
  it('publishes and immediately shows the QR from the publish step', async () => {
    mockApi.tailnetMobilePublish.mockResolvedValue({ ok: true, code: 'ok', detail: '' })
    mockApi.tailnetMobileQr.mockResolvedValue(qrPayload())
    await mount(data({ step: 'publish', published: false }))
    mockApi.tailnetMobile.mockResolvedValue(data())

    fireEvent.click(screen.getByRole('button', { name: /Set up & show QR/ }))

    await waitFor(() => expect(mockApi.tailnetMobilePublish).toHaveBeenCalled())
    expect(await screen.findByAltText('QR code linking to this dashboard')).toBeInTheDocument()
  })

  it('renders a refused publish as the server explained it', async () => {
    // A mutation can answer 200-with-ok:false; that detail is the only thing
    // telling the operator why nothing happened.
    mockApi.tailnetMobilePublish.mockResolvedValue({
      ok: false,
      code: 'occupied',
      detail: 'zzz something else is already served here',
    })
    await mount(data({ step: 'publish', published: false }))

    fireEvent.click(screen.getByRole('button', { name: /Set up & show QR/ }))

    expect(
      await screen.findByText('zzz something else is already served here'),
    ).toBeInTheDocument()
  })

  it('configures, restarts, publishes HTTPS, and shows the QR with one click', async () => {
    mockApi.patchConfig.mockResolvedValue({})
    mockApi.restartGateway.mockResolvedValue({})
    mockApi.tailnetMobilePublish.mockResolvedValue({ ok: true, code: 'ok', detail: '' })
    mockApi.tailnetMobileQr.mockResolvedValue(qrPayload())
    await mount(data({ step: 'trust_off', trusted: false }))
    mockApi.tailnetMobile
      .mockResolvedValueOnce(data({
        step: 'restart_gateway',
        trusted: true,
        startup_trusted: false,
        published: false,
      }))
      .mockResolvedValueOnce(data({ step: 'publish', published: false }))
      .mockResolvedValue(data())

    fireEvent.click(screen.getByRole('button', { name: /Set up & show QR/ }))

    await waitFor(() =>
      expect(mockApi.patchConfig).toHaveBeenCalledWith('dashboard.tailscale.enabled', true),
    )
    await waitFor(() => expect(mockApi.restartGateway).toHaveBeenCalled())
    await waitFor(() => expect(mockApi.tailnetMobilePublish).toHaveBeenCalled())
    expect(await screen.findByAltText('QR code linking to this dashboard')).toBeInTheDocument()
  })

  it('continues automatically after a required gateway restart', async () => {
    mockApi.restartGateway.mockResolvedValue({})
    mockApi.tailnetMobilePublish.mockResolvedValue({ ok: true, code: 'ok', detail: '' })
    mockApi.tailnetMobileQr.mockResolvedValue(qrPayload())
    await mount(data({ step: 'restart_gateway', startup_trusted: false }))
    mockApi.tailnetMobile
      .mockResolvedValueOnce(data({ step: 'publish', published: false }))
      .mockResolvedValue(data())

    fireEvent.click(screen.getByRole('button', { name: /Set up & show QR/ }))

    await waitFor(() => expect(mockApi.restartGateway).toHaveBeenCalled())
    await waitFor(() => expect(mockApi.tailnetMobilePublish).toHaveBeenCalled())
    expect(await screen.findByAltText('QR code linking to this dashboard')).toBeInTheDocument()
  })

  it('labels the full operation as phone access setup while it is running', async () => {
    mockApi.patchConfig.mockReturnValue(new Promise<void>(() => {}))
    await mount(data({ step: 'trust_off', trusted: false }))

    fireEvent.click(screen.getByRole('button', { name: /Set up & show QR/ }))

    const pending = await screen.findByRole('button', { name: /Setting up phone access/ })
    expect(pending).toBeDisabled()
  })

  it('re-checks from a mid-setup step', async () => {
    await mount(data({ step: 'sign_in', logged_in: false }))
    expect(mockApi.tailnetMobile).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: /Re-check/ }))

    await waitFor(() => expect(mockApi.tailnetMobile).toHaveBeenCalledTimes(2))
  })

  it('surfaces a transport error from a mutation', async () => {
    mockApi.patchConfig.mockRejectedValue(new Error('zzz network unreachable'))
    await mount(data({ step: 'trust_off', trusted: false }))

    fireEvent.click(screen.getByRole('button', { name: /Set up & show QR/ }))

    expect(await screen.findByText('zzz network unreachable')).toBeInTheDocument()
  })
})

describe('TailnetMobileCard — terminal steps', () => {
  it('offers no action at all when policy pins tailnet access off', async () => {
    await mount(data({ step: 'pinned', governance_pinned: true }))
    expect(screen.getByText('Blocked by policy')).toBeInTheDocument()
    // Nothing here is the operator's to change, so no button pretends otherwise.
    expect(screen.queryByRole('button', { name: /Re-check/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Set up & show QR/ })).toBeNull()
  })

  it('links to the Tailscale DNS console from enable_magicdns', async () => {
    await mount(data({ step: 'enable_magicdns', host: '' }))
    const link = screen.getByRole('link', { name: /Open Tailscale DNS settings/ })
    expect(link).toHaveAttribute('href', 'https://login.tailscale.com/admin/dns')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('requires tailnet HTTPS consent before offering one-click setup', async () => {
    await mount(data({ step: 'enable_https', published: false }))

    expect(screen.getByText('Enable HTTPS certificates in Tailscale')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Set up & show QR' })).toBeNull()
    expect(screen.queryByRole('img', { name: /QR code/i })).toBeNull()
    expect(
      screen.getByRole('link', { name: 'Open Tailscale HTTPS settings' }),
    ).toHaveAttribute('href', 'https://login.tailscale.com/admin/dns')
  })
})
