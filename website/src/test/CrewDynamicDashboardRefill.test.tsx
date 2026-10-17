// @vitest-environment jsdom
/**
 * A refetch that brings new VALUES for the SAME page must not reload the page.
 *
 * The instance version only moves when the page changes, so a read at the same
 * version is the same page with fresher numbers. The composed document already
 * listens for the host's refill (`kirocrew-dashboard:data`, see
 * `dashboard_frame._BOOTSTRAP_JS`) and every shipped template redraws on it, so
 * the frame should keep its document and post the new read into it. Minting a
 * new document instead reloads the whole iframe: the visible flicker.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { api, type DashboardManifest } from '../api/client'
import { renderWithProviders } from './helpers'
import CrewDynamicDashboard, {
  DATA_MESSAGE_TYPE,
  READY_MESSAGE_TYPE,
  islandRead,
} from '../pages/members/CrewDynamicDashboard'

/** Every srcdoc minted, newest last. A new entry is a new document in the frame. */
const mints: string[] = []

vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => {
    if (srcdoc && mints[mints.length - 1] !== srcdoc) mints.push(srcdoc)
    return {
      url: srcdoc ? `/sandbox-doc/${mints.indexOf(srcdoc)}` : null,
      pending: false,
      failed: false,
      stalled: false,
      retry: () => {},
    }
  },
}))

const MANIFEST = { id: 'project-report', version: 1, title: 'Project report', fields: [] } as unknown as DashboardManifest

/** A body the way `dashboard_frame.compose_body` shapes it: data island, then page. */
function composed(credits: number): string {
  const read = { fields: { credits }, agentic: [], seq: credits, stale: false, missing: [], written_at: {}, locale: 'en', subject: 'crewmate' }
  return (
    `<script type="application/json" id="kirocrew-dashboard-data">${JSON.stringify(read)}</script>` +
    `<p data-dashboard-field="credits">${credits}</p>`
  )
}

function page(credits: number) {
  return {
    instance_version: 3,
    template: { id: 'project-report', version: 1 },
    html: '<p>stored</p>',
    rendered_html: composed(credits),
    manifest: MANIFEST,
    state: 'live' as const,
  }
}

const DASHBOARD_KEY = ['member-dashboard', 'oncall', 'oncall']

describe('CrewDynamicDashboard refill in place', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    mints.length = 0
  })

  it('keeps the mounted document and posts the new values into it on a same-version refetch', async () => {
    const read = vi
      .spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page(12))
      .mockResolvedValue(page(31))
    const { queryClient } = renderWithProviders(
      <CrewDynamicDashboard target={{ kind: 'member', slug: 'oncall', member: 'oncall' }} displayName="On Call" />,
    )
    const frame = (await screen.findByTestId('crew-dashboard-iframe')) as HTMLIFrameElement
    const firstSrc = frame.getAttribute('src')
    expect(mints).toHaveLength(1)
    const win = frame.contentWindow
    expect(win).not.toBeNull()
    const posted = vi.spyOn(win as Window, 'postMessage')
    // The document says its page redraws on a refill, as every builtin does.
    beacon(win as Window, true)

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(1))

    // The page still gets the new values: the host refills the open document.
    await waitFor(() =>
      expect(posted).toHaveBeenCalledWith(
        expect.objectContaining({
          type: DATA_MESSAGE_TYPE,
          read: expect.objectContaining({ fields: { credits: 31 } }),
        }),
        '*',
      ),
    )
    // And it keeps its document: no second mint, same iframe element, same src.
    expect(mints).toHaveLength(1)
    const after = screen.getByTestId('crew-dashboard-iframe')
    expect(after).toBe(frame)
    expect(after.getAttribute('src')).toBe(firstSrc)
  })

  it('still loads a new document for a page that does not take refills, so it is never stale', async () => {
    const read = vi
      .spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page(12))
      .mockResolvedValue(page(31))
    const { queryClient } = renderWithProviders(
      <CrewDynamicDashboard target={{ kind: 'member', slug: 'oncall', member: 'oncall' }} displayName="On Call" />,
    )
    const frame = (await screen.findByTestId('crew-dashboard-iframe')) as HTMLIFrameElement
    const win = frame.contentWindow as Window
    const posted = vi.spyOn(win, 'postMessage')
    // Ready, but its own script never listens for a refill.
    beacon(win, false)

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(1))
    await waitFor(() => expect(mints.some((m) => m.includes('>31</p>'))).toBe(true))
    expect(mints).toHaveLength(2)
    expect(posted).not.toHaveBeenCalledWith(expect.objectContaining({ type: DATA_MESSAGE_TYPE }), '*')
  })

  it('ignores a refill claim from any window but the frame\'s own', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValueOnce(page(12)).mockResolvedValue(page(31))
    const { queryClient } = renderWithProviders(
      <CrewDynamicDashboard target={{ kind: 'member', slug: 'oncall', member: 'oncall' }} displayName="On Call" />,
    )
    const frame = (await screen.findByTestId('crew-dashboard-iframe')) as HTMLIFrameElement
    const posted = vi.spyOn(frame.contentWindow as Window, 'postMessage')
    // Another window says "refill" -- the frame's own document has said nothing.
    beacon(window, true)

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await waitFor(() => expect(mints.some((m) => m.includes('>31</p>'))).toBe(true))
    expect(posted).not.toHaveBeenCalled()
  })
})

describe('islandRead', () => {
  it('returns the island JSON the gateway escaped for HTML, unchanged', () => {
    // `dashboard_frame.escape_json_for_html` writes `<` as JSON's own \u003c.
    const html = '<script type="application/json" id="kirocrew-dashboard-data">{"fields":{"t":"\\u003c/script\\u003e"}}</script>'
    expect(islandRead(html)).toEqual({ fields: { t: '</script>' } })
  })

  it('is null for a page with no island or a broken one, so the frame re-mints', () => {
    expect(islandRead('<p>no island</p>')).toBeNull()
    expect(islandRead('<script type="application/json" id="kirocrew-dashboard-data">{nope</script>')).toBeNull()
    expect(islandRead('<script type="application/json" id="kirocrew-dashboard-data">[1]</script>')).toBeNull()
  })
})

/** The composed page's ready beacon, as `dashboard_frame._BEACON_JS` posts it. */
function beacon(source: Window, refill: boolean) {
  act(() => {
    window.dispatchEvent(new MessageEvent('message', { data: { type: READY_MESSAGE_TYPE, refill }, source }))
  })
}
