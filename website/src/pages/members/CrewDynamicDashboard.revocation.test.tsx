// @vitest-environment jsdom
/**
 * WITHDRAWING SCRIPT HAS TO TAKE EFFECT ON THE FRAME THAT IS MOUNTED.
 *
 * The server decides whether an owner-written page may run script and says so per read
 * (`scripts`). Withdrawing it is a governance act, so it cannot wait on anything the
 * PAGE does -- and the readiness beacon this component promotes a new layout on is
 * exactly that: a message the page sends, with script.
 *
 * Two ways that trapped a revocation, each leaving `allow-scripts` on the document a
 * reader is looking at:
 *
 *  - a NEW version answering `scripts: false` became a probating candidate, mounted
 *    with an empty sandbox to prove itself, so it could never send the beacon; the
 *    timeout only drew the "not the newest" band, and the scripted page it was meant to
 *    replace stayed mounted;
 *  - the SAME version answering `scripts: false` was dropped by the in-place swap, which
 *    compares the html and keeps the held document when it has not changed -- so the
 *    one thing that DID change never reached the frame.
 *
 * So the grant the frame is mounted with follows the server's latest answer, and a page
 * that may not run script is promoted without being asked to prove it came up.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { api, type DashboardManifest } from '../../api/client'
import { renderWithProviders } from '../../test/helpers'
import CrewDynamicDashboard from './CrewDynamicDashboard'

vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? `/sandbox-doc/${srcdoc.length}` : null,
    pending: false,
    failed: false,
    stalled: false,
    retry: () => {},
  }),
}))

const MANIFEST = { id: 'atlas-tree', version: 1, title: 'Tree', fields: [] } as unknown as DashboardManifest

/** One read. `scripts` left out means allowed, which is what an older gateway sends. */
function page(over: Record<string, unknown> = {}) {
  return {
    instance_version: 1,
    template: { id: 'atlas-tree', version: 1 },
    html: '<!doctype html><p>stored</p>',
    rendered_html: '<!doctype html><p>v1</p>',
    manifest: MANIFEST,
    state: 'live' as const,
    ...over,
  }
}

const TARGET = { kind: 'member', slug: 'atlas', member: 'Atlas' } as const
const DASHBOARD_KEY = ['member-dashboard', 'atlas']

const mount = () =>
  renderWithProviders(<CrewDynamicDashboard target={TARGET} displayName="Atlas" />)

describe('withdrawing script from a page already on screen', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('strips allow-scripts when a NEW version answers scripts: false', async () => {
    // The trapped case. Promotion on the beacon alone would leave the scripted
    // document mounted for as long as the tab stays open, because the page that was
    // meant to replace it has no script with which to beacon.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page())
      .mockResolvedValue(
        page({ instance_version: 2, rendered_html: '<!doctype html><p>v2</p>', scripts: false }),
      )
    const { queryClient } = mount()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      const grants = screen
        .getAllByTestId('crew-dashboard-iframe')
        .map((f) => f.getAttribute('sandbox'))
      expect(grants).not.toContain('allow-scripts')
    })
  })

  it('strips allow-scripts when the SAME version answers scripts: false', async () => {
    // No new layout at all -- only the flag moves. The in-place swap compares the html,
    // so nothing about the DOCUMENT changed and the revocation has to travel on its own.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page())
      .mockResolvedValue(page({ scripts: false }))
    const { queryClient } = mount()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      const grants = screen
        .getAllByTestId('crew-dashboard-iframe')
        .map((f) => f.getAttribute('sandbox'))
      expect(grants).not.toContain('allow-scripts')
    })
  })

  it('shows the newer static page rather than keeping the old scripted one', async () => {
    // The other half of the same fix: a static candidate is PROMOTED, not merely
    // de-scripted. Withholding it behind a beacon it cannot send would freeze the tab
    // on the previous layout for good.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page())
      .mockResolvedValue(
        page({ instance_version: 2, rendered_html: '<!doctype html><p>v2</p>', scripts: false }),
      )
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-iframe')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    // The kept-page band is what a never-promoted candidate leaves behind, so its
    // ABSENCE is how the promotion shows from outside.
    await waitFor(() => {
      const grants = screen
        .getAllByTestId('crew-dashboard-iframe')
        .map((f) => f.getAttribute('sandbox'))
      expect(grants).not.toContain('allow-scripts')
    })
    expect(screen.queryByTestId('crew-dashboard-kept-band')).not.toBeInTheDocument()
  })

  it('keeps allow-scripts while the server still permits it', async () => {
    // The revocation path must not become "static whenever anything changes".
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
  })
})


/**
 * THE ATTRIBUTE IS NOT THE GRANT, once a document is loaded.
 *
 * The tests above assert that `sandbox` loses `allow-scripts`, and they pass while the
 * revocation does nothing: under HTML, a frame's sandbox flags are computed when the
 * document is NAVIGATED, so rewriting the attribute on a frame that already holds a
 * running document leaves that document with the grants it was loaded under. It keeps
 * receiving pushed values and can still navigate itself to a host the ceiling now
 * forbids.
 *
 * So the grant has to be part of the frame's IDENTITY: a change of grant has to produce
 * a different element, which is a navigation. These tests assert the element is
 * replaced, which is the thing the attribute assertions cannot see.
 */
describe('a withdrawn grant reaches the document that is mounted', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  /** The same-version revocation, which is the one that changes nothing else at all. */
  it('remounts the frame when the grant narrows, rather than only rewriting sandbox', async () => {
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page())
      .mockResolvedValue(page({ scripts: false }))
    const { queryClient } = mount()
    const before = await screen.findByTestId('crew-dashboard-iframe')
    expect(before).toHaveAttribute('sandbox', 'allow-scripts')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      const after = screen.getByTestId('crew-dashboard-iframe')
      expect(after.getAttribute('sandbox')).toBe('')
      // IDENTITY, not the attribute: the same node with a rewritten attribute is a
      // document still running under the old grant.
      expect(after).not.toBe(before)
    })
  })

  it('remounts when a NEW version withdraws the grant', async () => {
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page())
      .mockResolvedValue(
        page({ instance_version: 2, rendered_html: '<!doctype html><p>v2</p>', scripts: false }),
      )
    const { queryClient } = mount()
    const before = await screen.findByTestId('crew-dashboard-iframe')
    expect(before).toHaveAttribute('sandbox', 'allow-scripts')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      const frames = screen.getAllByTestId('crew-dashboard-iframe')
      expect(frames.map((f) => f.getAttribute('sandbox'))).not.toContain('allow-scripts')
      expect(frames).not.toContain(before)
    })
  })

  it('does NOT remount while the grant is unchanged', async () => {
    // The other half, and the reason the key is the grant rather than the read: a frame
    // that remounted on every poll would throw away the page state the push exists to
    // keep -- a scroll position, an animation, a 3D camera -- once a minute.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    const { queryClient } = mount()
    const before = await screen.findByTestId('crew-dashboard-iframe')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(screen.getByTestId('crew-dashboard-iframe')).toBe(before)
  })
})


/**
 * AN EMPTY ANSWER IS AN ANSWER, and it has to reach the frame.
 *
 * `state: empty` is what the route sends when a bound package has nothing applied --
 * its applied version was pruned, or no person has approved one. The body then carries
 * no `rendered_html` and no `scripts` decision at all.
 *
 * The promote effect returns early on a body with no composed page, which is right for
 * what it does. What it must not leave behind is the PREVIOUS page: the held `good` and
 * the last `mayRunScript` stay, so the frame a reader is looking at goes on running
 * under the grant it loaded with. Pair that with an egress ceiling that has since been
 * restricted and the document can still navigate itself to a host now forbidden -- the
 * same hazard as a withdrawn grant that never reached the document, reached by a
 * different route.
 *
 * So an empty state drops the page rather than keeping it.
 */
describe('a read that answers empty', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  const empty = () => ({
    instance_version: 0,
    template: { id: '', version: 0 },
    dashboard: 'atlas-dashboard',
    html: '',
    manifest: {} as unknown as DashboardManifest,
    state: 'empty' as const,
    state_reason: 'the bound dashboard package has no applied version',
  })

  it('unmounts the page it was holding', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValueOnce(page()).mockResolvedValue(empty())
    const { queryClient } = mount()
    const before = await screen.findByTestId('crew-dashboard-iframe')
    expect(before).toHaveAttribute('sandbox', 'allow-scripts')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      expect(screen.queryAllByTestId('crew-dashboard-iframe')).toHaveLength(0)
    })
  })

  it('says the tab is empty rather than showing the stale page', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValueOnce(page()).mockResolvedValue(empty())
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-iframe')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    // The frame's own empty state, which is the honest answer for a bound package with
    // nothing applied -- not a page kept from a read that no longer describes it.
    await waitFor(() => {
      expect(screen.queryAllByTestId('crew-dashboard-iframe')).toHaveLength(0)
    })
  })

  it('keeps the page when the read still carries one', async () => {
    // The limit: a read is dropped for composing NO page, not for its state word. A
    // read that still composes one must not be turned into a blank tab by this rule.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    const { queryClient } = mount()
    const before = await screen.findByTestId('crew-dashboard-iframe')
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.getByTestId('crew-dashboard-iframe')).toBe(before)
  })
})

/**
 * A READ THAT GIVES THE TAB NO PAGE TO BE ON CANNOT LEAVE THE SCRIPTED ONE MOUNTED.
 *
 * `state: empty` above is one way to arrive here, and keying on that word alone left
 * two others open, both ending with `allow-scripts` on a document the record does not
 * have:
 *
 *  - the record's layout lock pins a `template_id` the catalog no longer holds, so the
 *    route resolves no package slug and composes nothing. The body carries neither
 *    `rendered_html` nor a `scripts` decision, and a guard that returned early on the
 *    missing page kept the held package frame -- its grant, its candidate and its
 *    probation state -- until something else made the tab reload;
 *  - a read moves the tab from a package to the crewmate's template instance. That
 *    body DOES compose a page, so the surface is the only thing that tells them apart:
 *    an instance version is per record and the html can coincide, so the version-keyed
 *    comparison kept the held package page and the template's never reached the frame.
 *
 * An absent `scripts` field is the absence of a decision in both, which is why it
 * cannot be read as the last decision.
 */
describe('a read that gives the tab no page to be on', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  /** The crewmate's own package page, mounted with script. */
  const packagePage = (over: Record<string, unknown> = {}) =>
    page({ dashboard: 'atlas-dashboard', ...over })

  /** A record whose locked template the catalog lost: same package, nothing composed. */
  const composesNothing = () => ({
    instance_version: 1,
    template: { id: 'atlas-tree', version: 1 },
    dashboard: 'atlas-dashboard',
    html: '<!doctype html><p>stored</p>',
    rendered_html: undefined,
    manifest: MANIFEST,
    state: 'live' as const,
  })

  it('drops the scripted page when the read composes nothing', async () => {
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(packagePage())
      .mockResolvedValue(composesNothing())
    const { queryClient } = mount()
    expect(await screen.findByTestId('crew-dashboard-iframe')).toHaveAttribute(
      'sandbox',
      'allow-scripts',
    )

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    // Not "no allow-scripts on the frame" but no such frame at all: the document is
    // gone, so there is nothing left holding the grant.
    await waitFor(() => {
      expect(screen.queryAllByTestId('crew-dashboard-iframe')).toHaveLength(0)
    })
    expect(
      screen
        .queryAllByTestId('crew-dashboard-iframe')
        .filter((f) => (f.getAttribute('sandbox') ?? '').includes('allow-scripts')),
    ).toHaveLength(0)
  })

  it('drops the scripted package page when the read moves the tab to the template', async () => {
    // The template read answers `scripts: false`, which is the combination that
    // matters: a ceiling that has since restricted the owner agent's egress. Keeping
    // the package page here would keep exactly the grant the server just refused.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(packagePage())
      .mockResolvedValue(
        page({
          dashboard: '',
          rendered_html: '<!doctype html><p>template instance</p>',
          scripts: false,
        }),
      )
    const { queryClient } = mount()
    expect(await screen.findByTestId('crew-dashboard-iframe')).toHaveAttribute(
      'sandbox',
      'allow-scripts',
    )

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      expect(
        screen
          .queryAllByTestId('crew-dashboard-iframe')
          .filter((f) => (f.getAttribute('sandbox') ?? '').includes('allow-scripts')),
      ).toHaveLength(0)
    })
    // And the template's page is what the tab shows, rather than a blank one: dropping
    // the held document must not cost the reader the page the read DID carry.
    await waitFor(() => {
      expect(screen.getByTestId('crew-dashboard-iframe')).toBeInTheDocument()
    })
  })

  it('drops the scripted package page when the template read answers error', async () => {
    // THE BRANCH THAT ANSWERED FIRST. `state: 'error'` keeps the held page on purpose
    // -- the read succeeded and says the NEWER stored copy does not parse, so the last
    // page that parsed stays under a band. But that reasoning is about a page on THIS
    // surface. A read that moves the tab to the template instance and reports `error`
    // used to take the keep-it branch before the surface was ever compared, so the
    // package document stayed mounted with `allow-scripts` on another surface's page.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(packagePage())
      .mockResolvedValue(
        page({
          dashboard: '',
          state: 'error',
          state_reason: 'manifest does not parse',
          rendered_html: undefined,
        }),
      )
    const { queryClient } = mount()
    expect(await screen.findByTestId('crew-dashboard-iframe')).toHaveAttribute(
      'sandbox',
      'allow-scripts',
    )

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      expect(
        screen
          .queryAllByTestId('crew-dashboard-iframe')
          .filter((f) => (f.getAttribute('sandbox') ?? '').includes('allow-scripts')),
      ).toHaveLength(0)
    })
  })

  it('keeps the page when an error read stays on the SAME surface', async () => {
    // The limit, and the behaviour main pins: `error` on this surface still keeps the
    // last page that parsed. The surface rule must not turn that into a blank tab.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(packagePage())
      .mockResolvedValue(
        packagePage({
          state: 'error',
          state_reason: 'manifest does not parse',
          rendered_html: undefined,
        }),
      )
    const { queryClient } = mount()
    const before = await screen.findByTestId('crew-dashboard-iframe')
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    // Waited on the BAND the error read raises, not on a timer: that band is the state
    // this read produces, so it is the barrier that says the read has been applied.
    await screen.findByTestId('crew-dashboard-broken-band')
    expect(screen.getByTestId('crew-dashboard-iframe')).toBe(before)
    expect(before).toHaveAttribute('sandbox', 'allow-scripts')
  })

  it('tells the surfaces apart when the version and the html both coincide', async () => {
    // THE CASE ONLY THE SURFACE SEPARATES. An instance version is per record and
    // restarts at 1, and two pages can compose to the same bytes -- so with the version
    // and the html equal, every comparison this component makes says "same page" and
    // the held document wins. Read in the under-granting direction, which is the one
    // that shows on screen: a STATIC package page, then the template instance, same
    // version and same bytes, which may run script. Carrying the package's grant
    // across mounts the template page static and the reader loses every chart on it.
    const SAME = '<!doctype html><p>same bytes</p>'
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(packagePage({ rendered_html: SAME, scripts: false }))
      .mockResolvedValue(page({ dashboard: '', rendered_html: SAME }))
    const { queryClient } = mount()
    expect(await screen.findByTestId('crew-dashboard-iframe')).toHaveAttribute('sandbox', '')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      expect(screen.getByTestId('crew-dashboard-iframe')).toHaveAttribute(
        'sandbox',
        'allow-scripts',
      )
    })
  })

  it('keeps a package page across a read for the SAME package', async () => {
    // The limit on the surface rule: it separates two surfaces, it does not re-mount a
    // page on every read of its own.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(packagePage())
    const { queryClient } = mount()
    const before = await screen.findByTestId('crew-dashboard-iframe')
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.getByTestId('crew-dashboard-iframe')).toBe(before)
  })
})
