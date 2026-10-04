// @vitest-environment jsdom
/**
 * The crewmate's dynamic dashboard frame: which page it reads, and what it shows
 * for each state the read can answer with.
 *
 * The page pipeline lives here rather than in the Members page's own test file,
 * because it is asynchronous in a way a page case cannot contain: the frame reads
 * the dashboard, then mints a sandbox document for it, and a mint settling after
 * its case has ended lands a state update on whichever case runs next. The page
 * file mocks this component and pins only the identity it passes in.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api, type DashboardManifest } from '../api/client'
import { renderWithProviders } from './helpers'
import CrewDynamicDashboard, { READY_MESSAGE_TYPE } from '../pages/members/CrewDynamicDashboard'

/**
 * Every srcdoc the component minted, newest LAST, and a url that CHANGES on each
 * distinct srcdoc and on each `retry()`.
 *
 * A constant stub url cannot tell a re-mint from no mint at all, and two of the
 * behaviours below are about exactly that: a page swapped in place keeps the frame
 * but changes its document, and the kept-page band's Retry re-mints the candidate.
 */
const mints: string[] = []
let retries = 0
const retrySpy = vi.fn(() => {
  retries += 1
})

vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => {
    if (srcdoc && mints[mints.length - 1] !== srcdoc) mints.push(srcdoc)
    return {
      url: srcdoc ? `/sandbox-doc/${mints.indexOf(srcdoc)}-${retries}` : null,
      pending: false,
      failed: false,
      stalled: false,
      retry: retrySpy,
    }
  },
}))

const MANIFEST: DashboardManifest = {
  id: 'project-report',
  version: 1,
  title: 'Project report',
  fields: [],
} as unknown as DashboardManifest

function page(over: Record<string, unknown> = {}) {
  return {
    instance_version: 1,
    template: { id: 'project-report', version: 1 },
    html: '<!doctype html><title>report</title><p>hello</p>',
    manifest: MANIFEST,
    state: 'live' as const,
    ...over,
  }
}

function mount() {
  return renderWithProviders(
    <CrewDynamicDashboard slug="oncall" member="oncall" displayName="On Call" />,
  )
}

/**
 * The dashboard read's own query key, for refetching JUST it.
 *
 * Scoped deliberately: the providers also mount the theme catalog and theme boot
 * queries against an unmocked api, so an unscoped `invalidateQueries()` awaits
 * refetches that never settle and the test times out instead of failing.
 */
const DASHBOARD_KEY = ['member-dashboard', 'oncall', 'oncall']

describe('CrewDynamicDashboard', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    mints.length = 0
    retries = 0
    retrySpy.mockClear()
  })

  it('reads the crewmate\'s own instance by slug AND exact member name', async () => {
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    await waitFor(() => expect(read).toHaveBeenCalledWith('oncall', 'oncall'))
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
  })

  it('shows the page the read resolved, in a frame granting scripts and nothing else', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
  })

  it('prefers the gateway-rendered page over the raw template when both are present', async () => {
    // The rendered page carries the folded VALUES; the raw html is the fallback,
    // so a frame that showed the raw one would show a page of empty cells.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      page({ rendered_html: '<!doctype html><title>filled</title>' }),
    )
    mount()
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
  })

  it('never paints "could not be loaded" over a page that loaded', async () => {
    // The read lands one commit before the page is held, so there is a window
    // where the query is no longer loading and no page is shown yet. A band
    // claiming the dashboard is unavailable must never be committed in it --
    // and a query run after the dust settles cannot see that, because both
    // commits flush together. So this watches the DOM for the whole mount and
    // asks what it EVER contained, not what it ended up containing.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    const seen = new Set<string>()
    const record = (node: Node) => {
      if (!(node instanceof HTMLElement)) return
      const own = node.getAttribute('data-testid')
      if (own) seen.add(own)
      node.querySelectorAll('[data-testid]').forEach((el) => {
        const id = el.getAttribute('data-testid')
        if (id) seen.add(id)
      })
    }
    const observer = new MutationObserver((records) => {
      for (const r of records) r.addedNodes.forEach(record)
    })
    observer.observe(document.body, { childList: true, subtree: true })
    mount()
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
    for (const r of observer.takeRecords()) r.addedNodes.forEach(record)
    observer.disconnect()
    record(document.body)
    expect([...seen]).toContain('crew-dashboard-frame')
    expect([...seen]).not.toContain('crew-dashboard-empty')
    expect([...seen]).not.toContain('crew-dashboard-error')
  })

  it('shows fresher VALUES for the same page version without waiting for probation', async () => {
    // The instance version only moves when the PAGE changes, so a refetch that brings
    // new fold values answers with the same version and different html -- and that is
    // the ordinary case, not an edge one: the default instance an unadopted crewmate
    // gets sits at version 0 forever, so for most crewmates the version NEVER moves.
    // Holding such a read back leaves the tab frozen on whatever it opened with.
    const read = vi
      .spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page({ html: '<!doctype html><p>12 credits</p>' }))
      .mockResolvedValue(page({ html: '<!doctype html><p>31 credits</p>' }))
    const { queryClient } = mount()
    await waitFor(() => expect(mints.some((m) => m.includes('12 credits'))).toBe(true))

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(1))
    await waitFor(() => expect(mints.some((m) => m.includes('31 credits'))).toBe(true))
    // Same version, so this was never a new page and nothing was withheld.
    expect(screen.queryByTestId('crew-dashboard-kept-band')).not.toBeInTheDocument()
  })

  it('lets the kept-page band\'s Retry promote the page it re-mints', async () => {
    // The band appears because a NEWER page did not beacon in time, which closes the
    // handshake's `settled` latch. Retry re-mints that candidate at a new url and the
    // fresh document beacons -- but only an effect that RE-RAN can hear it, because the
    // latch set by the timeout is still closed in the old one. So the button's whole
    // purpose depends on the re-mint being a dependency of the handshake, and the band
    // clearing is the only outward sign that the beacon was heard.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page({ instance_version: 1 }))
      .mockResolvedValue(page({ instance_version: 2, html: '<!doctype html><p>v2</p>' }))
    const { queryClient, rerender } = mount()
    await screen.findByTestId('crew-dashboard-frame')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    // No beacon for the candidate, so the readiness window lapses and the band lands.
    const band = await screen.findByTestId('crew-dashboard-kept-band', {}, { timeout: 9000 })
    const minted = mints.length

    fireEvent.click(within(band).getByRole('button'))
    await waitFor(() => expect(retrySpy).toHaveBeenCalled())
    // The re-mint is what the component must notice, so re-render to let it read the
    // new url the retry produced.
    rerender(<CrewDynamicDashboard slug="oncall" member="oncall" displayName="On Call" />)
    window.dispatchEvent(new MessageEvent('message', { data: { type: READY_MESSAGE_TYPE } }))
    await waitFor(() =>
      expect(screen.queryByTestId('crew-dashboard-kept-band')).not.toBeInTheDocument(),
    )
    // The promoted page is the candidate, so the frame now shows v2 rather than
    // merely having dropped the band.
    expect(mints.length).toBeGreaterThanOrEqual(minted)
    expect(mints.some((m) => m.includes('v2'))).toBe(true)
  }, 20000)

  it('says the read failed, with a way to try again', async () => {
    vi.spyOn(api, 'memberDashboard').mockRejectedValue(new Error('gateway hiccup'))
    mount()
    expect(await screen.findByTestId('crew-dashboard-error')).toBeInTheDocument()
    expect(screen.getByTestId('crew-dashboard-error-retry')).toBeInTheDocument()
  })

  it('says the dashboard is unavailable when the read resolves no page at all', async () => {
    // The read injects the default template for a crewmate that adopted nothing,
    // so an answer with no page means the registry did not load -- which is what
    // the copy names, rather than telling the reader to publish something.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(null)
    mount()
    expect(await screen.findByTestId('crew-dashboard-empty')).toBeInTheDocument()
    expect(screen.getByTestId('crew-dashboard-empty-retry')).toBeInTheDocument()
  })

  it('never draws the raw template when the stored copy does not parse', async () => {
    // The gateway answers 200 for `error` and `wire()` always carries `html`, but it
    // deliberately does NOT compose that state: no data island, no bootstrap, no
    // beacon. Falling back to `html` drew the template's placeholder markup as a
    // healthy dashboard -- every cell empty, nothing marked missing, no band.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      page({ state: 'error', state_reason: 'manifest does not parse', rendered_html: undefined }),
    )
    mount()
    await screen.findByTestId('crew-dashboard-broken')
    expect(screen.getByTestId('crew-dashboard-broken-retry')).toBeInTheDocument()
    // The frame is not drawn at all, and the template's own text never reaches the DOM.
    expect(screen.queryByTestId('crew-dashboard-frame')).toBeNull()
    expect(mints).toHaveLength(0)
    expect(document.body.textContent).not.toContain('hello')
  })

  it('keeps the last page that parsed, under a band, when a newer copy is broken', async () => {
    const read = vi
      .spyOn(api, 'memberDashboard')
      .mockResolvedValue(page({ rendered_html: '<!doctype html><title>filled</title>' }))
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-frame')
    const before = mints.length

    read.mockResolvedValue(
      page({
        instance_version: 2,
        state: 'error',
        state_reason: 'manifest does not parse',
        rendered_html: undefined,
      }),
    )
    await queryClient.refetchQueries({ queryKey: DASHBOARD_KEY })

    // Banded, never replaced: blanking a working page because a NEWER copy is broken
    // is the failure this keeps apart from "nothing to show".
    await screen.findByTestId('crew-dashboard-broken-band')
    expect(screen.getByTestId('crew-dashboard-frame')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-broken')).toBeNull()
    // The broken copy minted nothing, so the document on screen is still the good one.
    expect(mints).toHaveLength(before)
  })
})
