// @vitest-environment jsdom
/**
 * The crewmate's dynamic dashboard frame: which dashboard it reads, what it
 * shows for each state the read can answer with, and what it does with a block
 * patch.
 *
 * The page pipeline lives here rather than in the Members page's own test file,
 * because it is asynchronous in a way a page case cannot contain: the frame reads
 * the dashboard, then mints a sandbox document for it, and a mint settling after
 * its case has ended lands a state update on whichever case runs next. The page
 * file mocks this component and pins only the identity it passes in.
 *
 * ## THE CASES THAT MATTER MOST ARE THE ABSENCES
 *
 * `no default page` below is the whole of v3's "no default page" rule on this
 * side, and it is pinned as an absence because that is the shape of the
 * regression: the server is free to answer `empty` WITH a composed default
 * template -- it did exactly that before this change, and still does for a
 * crewmate on the template registry -- so one `if` here decides whether a person
 * who composed nothing is shown somebody else's layout filled with their own
 * numbers. A diff reintroducing it looks like a kindness.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api/client'
import { renderWithProviders } from './helpers'
import CrewDynamicDashboard, {
  DASHBOARD_FALLBACK_REFETCH_MS,
  READY_MESSAGE_TYPE,
} from '../pages/members/CrewDynamicDashboard'
import {
  DASHBOARD_BLOCK_PATCH_FRAME,
  PAGE_FULL_PAINT_MESSAGE_TYPE,
  PATCH_REASON_LAYOUT,
  __resetBlockPatchForTests,
  publishBlockPatch,
  type DashboardBlockPatch,
} from '../pages/members/dashboardBlockPush'
import { handleDashboardMoved } from '../hooks/useWebSocket'
import { LANG_STORAGE_KEY } from '../i18n/detect'

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

/** The v3 PACKAGE body, as the controller composes it: a package descriptor, the
 *  push counter the page starts from, the frame name, every block's values, and
 *  the composed document. */
function page(over: Record<string, unknown> = {}) {
  return {
    state: 'live' as const,
    instance_version: 3,
    package: {
      slug: 'oncall-dashboard',
      version: 3,
      layout_fingerprint: 'sha256:abc',
      bound_to: 'crewmate:oncall',
    },
    push_version: 0,
    push_frame: DASHBOARD_BLOCK_PATCH_FRAME,
    blocks: { prs: { open_prs: 12 }, notes: { my_call: 'ship it' } },
    missing: [],
    rendered_html: '<!doctype html><title>report</title><p>hello</p>',
    ...over,
  }
}

/** The renderer's block-patch type, distinct from the full-paint one. */
const PATCH_TYPE = 'kirocrew-dashboard:block-patch'

/** A renderer payload labelled so a case can tell its own frame from a sibling's.
 *  The message carries no version, so the label is how the two are told apart. */
function payload(label: string, over: Record<string, unknown> = {}) {
  return {
    type: PATCH_TYPE,
    blocks: { prs: { fields: { open_prs: 31 }, display: { open_prs: label } } },
    seq: 44,
    stale: false,
    missing: [],
    ...over,
  }
}

/** A frame as the controller sends it, defaulted to fit `page()`.
 *
 *  `blocks` names the fields that moved and is derived from `patch` by the
 *  server; `patch` is the renderer's own payload, forwarded verbatim. */
function patch(over: Partial<DashboardBlockPatch> = {}): DashboardBlockPatch {
  return {
    slug: 'oncall',
    dashboard: 'oncall-dashboard',
    version: 1,
    layout: 3,
    fold: 'work',
    blocks: { prs: ['open_prs'] },
    missing: [],
    patch: {
      type: PATCH_TYPE,
      blocks: { prs: { fields: { open_prs: 31 }, display: { open_prs: '31' } } },
      seq: 44,
      stale: false,
      missing: [],
    },
    refetch: false,
    reason: '',
    ...over,
  }
}

function mount() {
  return renderWithProviders(
    <CrewDynamicDashboard slug="oncall" member="oncall" displayName="On Call" />,
  )
}

/**
 * Give the mounted frame a `contentWindow` with a spy, since jsdom never loads
 * the stub url and the component posts into whatever is there.
 *
 * FLUSHES BEFORE RETURNING, and that is not belt-and-braces. The patch listener
 * closes over the page on screen, so the component re-subscribes on the commit
 * that promotes one -- and `findByTestId` resolves ON that commit, before its
 * effects have run. A patch published the instant this returns can therefore
 * reach the PREVIOUS closure, which has no page and drops it. Every case in the
 * block-patch group was open to that; one of them failed in a full-file run
 * while passing alone, which is how it surfaced.
 */
async function spyOnFrame() {
  const frame = await screen.findByTestId('crew-dashboard-iframe')
  const postMessage = vi.fn()
  Object.defineProperty(frame, 'contentWindow', {
    value: { postMessage },
    configurable: true,
  })
  await flushEffects()
  return postMessage
}

/**
 * Let every pending effect and the state updates it schedules run.
 *
 * Needed by the absence cases below, and the reason is worth stating because it
 * is what a mutation run revealed: an element committed in the SAME pass as the
 * read -- the empty state is one -- is not a signal that the component has
 * finished deciding. The promotion effect runs after that commit, so an
 * assertion placed right after `findByTestId` asks its question one tick too
 * early and passes however the component goes on to behave.
 *
 * Macrotask TURNS rather than a duration, because the links are what the wait is
 * about and a millisecond count is a guess at how long they take on the machine
 * that happens to be running. Each `setTimeout(_, 0)` yields exactly one turn,
 * so the loop below waits for the chain's length and nothing more -- and it
 * cannot get faster or slower than the chain it is derived from.
 *
 * Every caller of this helper asserts an ABSENCE (no mint, no iframe, no band),
 * which is the one shape `findBy*` cannot express: there is no element whose
 * arrival ends the wait, so the wait has to be bounded by the work instead.
 */
const EFFECT_CHAIN_TURNS = 5

async function flushEffects() {
  await act(async () => {
    for (let turn = 0; turn < EFFECT_CHAIN_TURNS; turn += 1) {
      await new Promise(resolve => setTimeout(resolve, 0))
    }
  })
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
    __resetBlockPatchForTests()
  })

  it('reads the crewmate\'s own dashboard by slug AND exact member name', async () => {
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    await waitFor(() => expect(read).toHaveBeenCalledWith('oncall', 'oncall', 'en'))
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
  })

  it('asks for the page in the UI language the reader chose', async () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')
    try {
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      await waitFor(() => expect(read).toHaveBeenCalledWith('oncall', 'oncall', 'zh-CN'))
    } finally {
      localStorage.removeItem(LANG_STORAGE_KEY)
    }
  })

  it('shows the page the read resolved, in a frame granting scripts and nothing else', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
  })

  it('marks the frame with the LAYOUT version the document was composed at', async () => {
    // The number a patch's own `layout` is compared against, so it is on the
    // element a reader of the DOM can see: a page showing values from one layout
    // under another layout's blocks is the failure this whole comparison exists
    // to prevent, and it is invisible in a screenshot.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    expect(await screen.findByTestId('crew-dashboard-iframe')).toHaveAttribute(
      'data-layout-version',
      '3',
    )
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

  it('shows fresher VALUES at the same layout without waiting for probation', async () => {
    // A dashboard artifact versions only when `model` / `view` / `theme` change, so
    // a refetch that brings new fold values answers at the same layout with a
    // different document -- and that is the ordinary case, not an edge one.
    // Holding such a read back leaves the tab frozen on whatever it opened with.
    const read = vi
      .spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page({ rendered_html: '<!doctype html><p>12 credits</p>' }))
      .mockResolvedValue(page({ rendered_html: '<!doctype html><p>31 credits</p>' }))
    const { queryClient } = mount()
    await waitFor(() => expect(mints.some((m) => m.includes('12 credits'))).toBe(true))

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(1))
    await waitFor(() => expect(mints.some((m) => m.includes('31 credits'))).toBe(true))
    // Same layout, so this was never a new page and nothing was withheld.
    expect(screen.queryByTestId('crew-dashboard-kept-band')).not.toBeInTheDocument()
  })

  it('puts a CHANGED TEMPLATE page on probation, not straight onto the screen', async () => {
    // A template page has no package, so the package version cannot be what
    // probation compares: it is 0 for every one of them, which made two
    // different template pages look like the same page and swapped a changed
    // one on screen without it proving it loads. Applying or rolling back a
    // template is an ordinary operation, so this is the common path, and the
    // page a reader is looking at is what a failed swap costs.
    const tpl = (over: Record<string, unknown> = {}) => ({
      state: 'live' as const,
      instance_version: 3,
      template: { id: 'project-report', version: 1 },
      push_version: 0,
      blocks: {},
      missing: [],
      rendered_html: '<!doctype html><title>report</title><p>v1 page</p>',
      ...over,
    })
    const read = vi
      .spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(tpl())
      .mockResolvedValue(
        tpl({
          template: { id: 'project-report', version: 2 },
          rendered_html: '<!doctype html><title>report</title><p>v2 page</p>',
        })
      )
    const { queryClient } = mount()
    await waitFor(() => expect(mints.some((m) => m.includes('v1 page'))).toBe(true))

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(1))
    // The new template version IS a new page, so it is minted on probation --
    // which is observable as the candidate being built and mounted at all.
    await waitFor(() => expect(mints.some((m) => m.includes('v2 page'))).toBe(true))
    expect(await screen.findByTestId('crew-dashboard-probe')).toBeInTheDocument()
  })

  it('lets the kept-page band\'s Retry promote the page it re-mints', async () => {
    // The band appears because a NEWER layout did not beacon in time, which closes
    // the handshake's `settled` latch. Retry re-mints that candidate at a new url
    // and the fresh document beacons -- but only an effect that RE-RAN can hear it,
    // because the latch set by the timeout is still closed in the old one. So the
    // button's whole purpose depends on the re-mint being a dependency of the
    // handshake, and the band clearing is the only outward sign the beacon landed.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page())
      .mockResolvedValue(
        page({
          package: {
            slug: 'oncall-dashboard',
            version: 4,
            layout_fingerprint: 'sha256:def',
            bound_to: 'crewmate:oncall',
          },
          rendered_html: '<!doctype html><p>v2</p>',
        }),
      )
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

  it('calls a failed read LOADED, and a refused page DRAWN', async () => {
    // Two dead ends that read the same tell a reader nothing about which one they
    // are in. The read failing means no page ever arrived, so "loaded" is the true
    // word; "drawn" belongs to the branch where a page DID arrive and would not
    // render. Asserted on the rendered sentences rather than on the keys, because
    // the defect was the two branches pointing at the same key.
    vi.spyOn(api, 'memberDashboard').mockRejectedValue(new Error('gateway hiccup'))
    mount()
    const failed = await screen.findByTestId('crew-dashboard-error')
    expect(failed.textContent).toContain('could not be loaded')
    expect(failed.textContent).not.toContain('could not be drawn')
    // Neither sentence may send the reader to an action this surface has no control
    // for: the one button is Try again, so "or reload the page" is gone.
    expect(failed.textContent).not.toContain('reload the page')
  })

  it('says the dashboard is unavailable when the read resolves no body at all', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(null)
    mount()
    expect(await screen.findByTestId('crew-dashboard-empty')).toBeInTheDocument()
    expect(screen.getByTestId('crew-dashboard-empty-retry')).toBeInTheDocument()
  })

  it('never draws a page when the stored dashboard does not parse', async () => {
    // The controller answers 200 for `error` but deliberately does not compose that
    // state: no data island, no bootstrap, no beacon. Drawing anything here would
    // show placeholder markup as a healthy dashboard -- every cell empty, nothing
    // marked missing, no band.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      page({ state: 'error', state_reason: 'package does not parse', rendered_html: undefined }),
    )
    mount()
    await screen.findByTestId('crew-dashboard-broken')
    expect(screen.getByTestId('crew-dashboard-broken-retry')).toBeInTheDocument()
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
      page({ state: 'error', state_reason: 'package does not parse', rendered_html: undefined }),
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

  describe('live refresh', () => {
    it('re-renders the tab when a crewmate writes a value, with no reload', async () => {
      // THE WHOLE CHAIN, end to end: the gateway's `dashboard_value_written` frame,
      // the handler the socket routes it to, the query key it invalidates, the refetch
      // that follows and the new document the frame mints from it. Driven through the
      // real handler rather than by calling `invalidateQueries` here, because an
      // invalidation written by the test proves react-query works and says nothing
      // about whether the frame reaches it.
      const read = vi
        .spyOn(api, 'memberDashboard')
        .mockResolvedValueOnce(page({ rendered_html: '<!doctype html><p>nothing needs you</p>' }))
        .mockResolvedValue(page({ rendered_html: '<!doctype html><p>approve the plan</p>' }))
      const { queryClient } = mount()
      await waitFor(() => expect(mints.some((m) => m.includes('nothing needs you'))).toBe(true))
      const before = read.mock.calls.length

      handleDashboardMoved(queryClient, { slug: 'oncall' })

      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(before))
      await waitFor(() => expect(mints.some((m) => m.includes('approve the plan'))).toBe(true))
      // The frame is the SAME element across the swap, which is what "without a
      // reload" means here: the document inside it changed, the tab did not remount.
      expect(screen.getByTestId('crew-dashboard-frame')).toBeInTheDocument()
    })

    it('leaves another crewmate\'s open tab alone', async () => {
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      const { queryClient } = mount()
      await waitFor(() => expect(read).toHaveBeenCalled())
      const before = read.mock.calls.length

      handleDashboardMoved(queryClient, { slug: 'release-captain' })
      // The foreign frame must cause no read, which is an absence and so has no state
      // to wait for. A sleep here would be a guess at how long to watch, so a frame for
      // THIS crewmate follows it as a positive control: once that read lands, both
      // frames have been through the same handler, and the count having risen by
      // exactly one is what proves the first added nothing.
      handleDashboardMoved(queryClient, { slug: 'oncall' })

      await waitFor(() => expect(read.mock.calls.length).toBe(before + 1))
    })

    it('keeps a finite fallback interval under the push path', async () => {
      // A missed frame -- a dropped socket, a fold that advanced while the tab was
      // closed -- must not freeze the page forever. Asserted on the constant because
      // every case that drives a frame passes with no interval at all.
      expect(Number.isFinite(DASHBOARD_FALLBACK_REFETCH_MS)).toBe(true)
      expect(DASHBOARD_FALLBACK_REFETCH_MS).toBeGreaterThan(0)
    })
  })

  describe('the block patch', () => {
    it('posts a fitting patch into the document instead of re-reading', async () => {
      // THE POINT OF THE WHOLE PUSH PATH. A fold advanced, the controller composed
      // the one block that subscribes to it, and the page hands those values to the
      // document -- no refetch, so the tab does not pay for a whole recomposed
      // page to learn one number.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      const postMessage = await spyOnFrame()
      const before = read.mock.calls.length

      publishBlockPatch(patch())

      await waitFor(() => expect(postMessage).toHaveBeenCalled())
      // THE DOCUMENT'S OWN REFILL SHAPE: its listener matches this type exactly and
      // does `read = freeze(data.read)`, so this is the whole message. `blocks` is
      // not forwarded -- the document re-fills itself.
      // VERBATIM: the very object off the frame, not a message built here. And
      // NOT the full-paint type -- that listener re-initialises every block.
      expect(postMessage.mock.calls[0][0]).toEqual(patch().patch)
      expect(postMessage.mock.calls[0][0].type).toBe(PATCH_TYPE)
      expect(postMessage.mock.calls[0][0].type).not.toBe(PAGE_FULL_PAINT_MESSAGE_TYPE)
      // `'*'`: the document is sandboxed without `allow-same-origin`, so its opaque
      // origin cannot be named.
      expect(postMessage.mock.calls[0][1]).toBe('*')
      expect(read.mock.calls.length).toBe(before)
    })

    it('hands over the renderer\'s formatted strings without touching them', async () => {
      // The page shows `read.display`, which the RENDERER formatted. Nothing in
      // TypeScript parses, rounds or localises a value: a second formatter is how a
      // reader ends up looking at a bare `1200000000` under a label that said
      // `1.2 GB` a second earlier, on the same page, from the same number.
      vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      const postMessage = await spyOnFrame()

      publishBlockPatch(
        patch({
          patch: {
            type: PATCH_TYPE,
            blocks: { disk: { fields: { disk: 1200000000 }, display: { disk: '1.2 GB' } } },
            seq: 9,
            stale: false,
            missing: [],
          },
        }),
      )

      await waitFor(() => expect(postMessage).toHaveBeenCalled())
      expect(postMessage.mock.calls[0][0].blocks.disk.display).toEqual({ disk: '1.2 GB' })
      expect(postMessage.mock.calls[0][0].blocks.disk.fields).toEqual({ disk: 1200000000 })
    })

    it('re-reads rather than forwarding an empty payload', async () => {
      // The server sends no frame at all when nothing subscribes, so a frame shaped
      // like this is a gateway and a bundle that disagree. Forwarding it would post
      // a typeless message the document drops in silence, while this side spent a
      // version on it and moved past the gap it should have re-read for.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      const postMessage = await spyOnFrame()
      const before = read.mock.calls.length

      publishBlockPatch(patch({ patch: {} }))

      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(before))
      expect(postMessage).not.toHaveBeenCalled()
    })

    it('re-reads instead of applying when the LAYOUT moved', async () => {
      // A recompose produces a perfectly contiguous version, so the layout check is
      // the only thing that catches it -- and applying would paint the new layout's
      // values into the old layout's blocks.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      const postMessage = await spyOnFrame()
      const before = read.mock.calls.length

      publishBlockPatch(patch({ layout: 4 }))

      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(before))
      expect(postMessage).not.toHaveBeenCalled()
    })

    it('re-reads on a refetch frame and applies none of it', async () => {
      // The server already decided. A refetch frame carries no values at all, so
      // there is nothing to apply even though its numbers line up.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      const postMessage = await spyOnFrame()
      const before = read.mock.calls.length

      publishBlockPatch(patch({ refetch: true, blocks: {}, patch: {}, fold: '', reason: PATCH_REASON_LAYOUT }))

      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(before))
      expect(postMessage).not.toHaveBeenCalled()
    })

    it('re-reads on a version GAP', async () => {
      // The read seeded the counter at 0, so version 3 means frames 1 and 2 never
      // arrived and the blocks on screen are not what the server composed.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      const postMessage = await spyOnFrame()
      const before = read.mock.calls.length

      publishBlockPatch(patch({ version: 3 }))

      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(before))
      expect(postMessage).not.toHaveBeenCalled()
    })

    it('re-reads a LOWER version instead of posting it, which is the restart case', async () => {
      // The push counter is in memory on the server, per live page, so a gateway
      // restart arms a fresh page at 0 and the next frame arrives BELOW what this
      // tab holds. Read as a replay it would be dropped, and so would every frame
      // after it -- the tab frozen on pre-restart values with nothing saying so.
      //
      // The tab holds 9 (from `push_version`) and the frame says 1.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ push_version: 9 }))
      mount()
      const postMessage = await spyOnFrame()
      const before = read.mock.calls.length

      publishBlockPatch(patch({ version: 1, patch: payload('after the restart') }))

      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(before))
      expect(postMessage).not.toHaveBeenCalled()
    })

    it('re-reads a repeat of the frame it already applied', async () => {
      // Strict equality: a duplicate is not special-cased either. One extra read is
      // the cheaper mistake than a branch that also swallows a restart.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ push_version: 4 }))
      mount()
      const postMessage = await spyOnFrame()
      const before = read.mock.calls.length

      publishBlockPatch(patch({ version: 4, patch: payload('the duplicate') }))

      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(before))
      expect(postMessage).not.toHaveBeenCalled()
    })

    it('re-reads a patch naming a block the page is not showing', async () => {
      // The page and the controller disagree about the view while `layout` says they
      // do not -- the one case that number cannot catch.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      const postMessage = await spyOnFrame()
      const before = read.mock.calls.length

      publishBlockPatch(patch({ blocks: { ghost: ['x'] } }))

      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(before))
      expect(postMessage).not.toHaveBeenCalled()
    })

    it('takes its position back from the READ after a gap, not from the bad frame', async () => {
      // A refetch does NOT advance `held` from the frame that failed the check.
      // `held` comes from the body (`push_version`, `package.version`), because a
      // refetch exists for the case where what this tab holds cannot be trusted --
      // carrying the untrustworthy half forward would defeat it.
      //
      // The read re-seeds at 3 here, so the frame after the gap is 4 and applies.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ push_version: 3 }))
      mount()
      const postMessage = await spyOnFrame()
      const afterMount = read.mock.calls.length

      // 5 against a held 3: a gap, so a re-read rather than a post.
      publishBlockPatch(patch({ version: 5 }))
      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(afterMount))
      expect(postMessage).not.toHaveBeenCalled()

      // The re-read put it back at 3, so 4 is the very next frame and applies.
      publishBlockPatch(patch({ version: 4, patch: payload('after the gap') }))
      await waitFor(() => expect(postMessage).toHaveBeenCalled())
      expect(postMessage.mock.calls[0][0].blocks.prs.display).toEqual({ open_prs: 'after the gap' })
    })

    it('leaves another crewmate\'s patch alone', async () => {
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      const postMessage = await spyOnFrame()
      const before = read.mock.calls.length

      publishBlockPatch(patch({ slug: 'release-captain', patch: payload('the foreign one') }))
      // Positive control, as above: this crewmate's own patch must still post.
      publishBlockPatch(patch())

      await waitFor(() => expect(postMessage).toHaveBeenCalledTimes(1))
      expect(postMessage.mock.calls[0][0].blocks.prs.display).toEqual({ open_prs: '31' })
      expect(read.mock.calls.length).toBe(before)
    })

    it('says so out loud when the gateway pushes a frame name this build does not listen for', async () => {
      // A renamed frame turns the push path off, and a silent push path is
      // indistinguishable from a quiet crew log: no error, no red, just a page that
      // stops moving. The controller names the type in the body for exactly this
      // comparison, so the disagreement is reported where a developer sees it.
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
      vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ push_frame: 'dashboard_blocks_v9' }))
      mount()
      await screen.findByTestId('crew-dashboard-frame')
      await waitFor(() => expect(warn).toHaveBeenCalled())
      expect(String(warn.mock.calls[0][0])).toContain('dashboard_blocks_v9')
      expect(String(warn.mock.calls[0][0])).toContain(DASHBOARD_BLOCK_PATCH_FRAME)
    })

    it('stays quiet when the names agree', async () => {
      // The control for the case above: a warning on every healthy mount would train
      // a reader to ignore it.
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
      vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      mount()
      await screen.findByTestId('crew-dashboard-frame')
      expect(warn).not.toHaveBeenCalled()
    })
  })
})

describe('no default page', () => {
  // v3's rule: a dashboard exists once the agent writes a layout, and until then
  // the empty state IS the answer. The rule cannot live in the server alone --
  // the body is free to carry a composed page for any state, and the template
  // registry path STILL composes a default template for a crewmate who adopted
  // nothing. These are the cases that decline to draw it.
  beforeEach(() => {
    vi.restoreAllMocks()
    mints.length = 0
    retries = 0
    retrySpy.mockClear()
    __resetBlockPatchForTests()
  })

  it('reports nothing composed as a STATE, with no retry', async () => {
    // Nothing composed and nothing wrong are different answers. Rendering this as a
    // failure would put a permanent error on every crewmate's tab and offer a Try
    // again that re-reads the same empty answer forever -- which reads as a fault
    // the reader could clear. Asserted together: the state's own line IS there, and
    // neither the failure notice nor its retry is.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue({
      state: 'empty',
      state_reason: 'no dashboard package',
      instance_version: 0,
    })
    mount()
    expect(await screen.findByTestId('crew-dashboard-none')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-empty')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-empty-retry')).toBeNull()
  })

  it('DRAWS NOTHING when the body carries a composed page for the empty state', async () => {
    // THE REGRESSION THIS FILE EXISTS FOR.
    //
    // The route answered `empty` WITH a rendered default template, reasoning that
    // "an empty frame answers none of the questions a person opened the tab with".
    // The old promotion accepted it, because it refused only `error`. So a
    // crewmate who had composed nothing was shown a full dashboard of a shipped
    // layout, carrying their own fold values, with `instance_version` 0 and
    // nothing on it saying it was not theirs.
    //
    // The body here is exactly that: `empty`, and a page. Nothing may be minted
    // from it, and the template's own text must not reach the DOM.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue({
      state: 'empty',
      state_reason: 'no dashboard package',
      instance_version: 0,
      template: { id: 'project-report', version: 1 },
      rendered_html: '<!doctype html><title>default</title><p>someone else\'s layout</p>',
      html: '<!doctype html><p>stored</p>',
    })
    mount()
    // SETTLE FIRST, on a positive signal. `waitFor` resolves the moment its callback
    // stops throwing, so waiting for the frame to be ABSENT succeeds on the first
    // poll -- before the read has even landed -- and the case would pass whatever
    // the component went on to do.
    expect(await screen.findByTestId('crew-dashboard-none')).toBeInTheDocument()
    // AND THEN FLUSH, which is the part a mutation run is needed to discover.
    // `crew-dashboard-none` is committed in the same pass as the read, so finding
    // it proves the read landed and NOT that the component has finished deciding:
    // the promotion effect runs after that commit. Without this flush, deleting
    // the promotion's own empty gate left every assertion below still green,
    // because the page was minted one tick after they ran.
    await flushEffects()
    // `mints` is the detector that survives a second gate. Even with the render
    // branch refusing to draw, a promoted page is a MINTED page: the srcdoc is
    // built during render from whatever was promoted, so this sees a fallback that
    // got as far as being prepared, not merely one that got as far as the screen.
    expect(mints).toEqual([])
    expect(screen.queryByTestId('crew-dashboard-frame')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-iframe')).toBeNull()
    expect(document.body.textContent).not.toContain("someone else's layout")
    // Still the empty state after the flush, so this is the settled answer rather
    // than a frame the component was about to replace.
    expect(screen.getByTestId('crew-dashboard-none')).toBeInTheDocument()
  })

  it('and goes on drawing nothing on a package-bound member\'s SECOND empty read, which is where the promotion gate holds', async () => {
    // THE CASE THAT MAKES THE PROMOTION'S OWN GATE LOAD-BEARING, found by
    // mutating it away and watching every other case stay green.
    //
    // Two gates refuse an `empty` body independently: the promotion refuses to
    // hold it, and the clearing effect drops whatever is held. On the FIRST empty
    // read the clearing effect covers for the promotion, so deleting the
    // promotion's gate changes nothing visible -- which is exactly how such a
    // line gets deleted as redundant.
    //
    // It is not redundant on the second one. The clearing effect is keyed on
    // `data.state`, so a second `empty` read does not re-run it: the state string
    // did not change. The promotion effect is keyed on the whole body and DOES
    // re-run. And a second empty read is the ordinary case, not a contrived one --
    // the tab's fallback interval re-reads on its own, and for a crewmate with no
    // dashboard every one of those answers `empty`.
    // PACKAGE-BOUND on purpose, so this one case pins the promotion gate AND
    // pins it on the shape the server-side scoping makes the dangerous one: a
    // member whose dashboard comes from a package, reported `empty`, sent a
    // composed page anyway.
    const bound = {
      state: 'empty' as const,
      state_reason: 'package bound but not composed',
      instance_version: 0,
      package: {
        slug: 'oncall-dashboard',
        version: 4,
        layout_fingerprint: 'sha256:abc',
        bound_to: 'crewmate:oncall',
      },
      push_version: 0,
      push_frame: DASHBOARD_BLOCK_PATCH_FRAME,
      blocks: { prs: ['open_prs'] },
      missing: [],
    }
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue({
      ...bound,
      rendered_html: '<!doctype html><p>default on the first read</p>',
    })
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-none')
    await flushEffects()

    // The same state, a different body -- which is what a re-read of a composed
    // default looks like, since its values move even while its layout does not.
    read.mockResolvedValue({
      ...bound,
      rendered_html: '<!doctype html><p>default on the second read</p>',
    })
    await queryClient.refetchQueries({ queryKey: DASHBOARD_KEY })
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(1))
    await flushEffects()

    expect(mints).toEqual([])
    expect(screen.getByTestId('crew-dashboard-none')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-iframe')).toBeNull()
    expect(document.body.textContent).not.toContain('default on the second read')
  })

  it('DRAWS NOTHING for a PACKAGE-BOUND empty member that is sent a page anyway', async () => {
    // THE PRECISE REGRESSION THE TWO HALVES ARE SCOPED AROUND, and the one the
    // other cases in this block do not cover: every fixture above is either the
    // template-registry shape (`template:`) or carries no package key at all.
    //
    // The server half of the fix is SCOPED: the controller suppresses the builtin
    // fallback only for a member whose dashboard comes from a PACKAGE
    // (`fallback = None if packaged.bound else default_instance(slug)`), so the
    // shipped templates keep working for everyone else. That scoping is exactly
    // what makes this body the dangerous one: a package-bound member, `empty`,
    // and a composed page -- which is the shape the server would start sending
    // again if that condition were ever widened, inverted, or lost in a merge.
    //
    // So this body is the one the frontend must refuse on its own authority, with
    // no help from the server. `bound_to` names this crewmate and a layout version
    // is present, so nothing about it looks like the registry path.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue({
      state: 'empty',
      state_reason: 'package bound but not composed',
      instance_version: 0,
      package: {
        slug: 'oncall-dashboard',
        version: 4,
        layout_fingerprint: 'sha256:abc',
        bound_to: 'crewmate:oncall',
      },
      push_version: 0,
      push_frame: DASHBOARD_BLOCK_PATCH_FRAME,
      blocks: { prs: ['open_prs'] },
      missing: [],
      rendered_html: '<!doctype html><title>default</title><p>a layout nobody here composed</p>',
    })
    mount()
    expect(await screen.findByTestId('crew-dashboard-none')).toBeInTheDocument()
    await flushEffects()
    expect(mints).toEqual([])
    expect(screen.queryByTestId('crew-dashboard-iframe')).toBeNull()
    expect(document.body.textContent).not.toContain('a layout nobody here composed')
    // And the empty state is still what is on screen after the flush, so this is
    // the settled answer rather than a frame about to replace it.
    expect(screen.getByTestId('crew-dashboard-none')).toBeInTheDocument()
  })

  it('does not hold the skeleton up waiting for a page that is not coming', async () => {
    // The other half. The readiness gate used to wait on `rendered_html` alone, so
    // a body carrying a page for a state that never promotes left the loading
    // skeleton on screen for good -- and for `empty`, which is every crewmate's
    // first answer, that skeleton would BE the feature.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue({
      state: 'empty',
      instance_version: 0,
      rendered_html: '<!doctype html><p>default</p>',
    })
    mount()
    expect(await screen.findByTestId('crew-dashboard-none')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-loading')).toBeNull()
  })

  it('DROPS a page already on screen when a later read says empty', async () => {
    // "Keep the last good page" is for a page that FAILED, where the previous one
    // is still the best answer about the same dashboard. `empty` is not that: the
    // dashboard is gone, so the page on screen is a layout that no longer exists
    // under the name of a crewmate who no longer has one. Holding it would
    // reintroduce the default page from the other direction -- not a builtin
    // served to someone with nothing, but a deleted one that never stops being
    // served, which is worse because it reads as current.
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-frame')

    read.mockResolvedValue({ state: 'empty', state_reason: 'package deleted', instance_version: 0 })
    await queryClient.refetchQueries({ queryKey: DASHBOARD_KEY })

    expect(await screen.findByTestId('crew-dashboard-none')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-frame')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-kept-band')).toBeNull()
  })

  it('and does not resurrect it when a NEW dashboard is composed later', async () => {
    // THE CASE THAT MAKES THE DROP LOAD-BEARING, and the one the case above cannot
    // see: with the render branch refusing to draw an `empty` state anyway, merely
    // asserting the frame is gone says nothing about whether the old page is still
    // HELD behind it. A mutation that neutered the clearing effect left the case
    // above green.
    //
    // Here the crewmate composes a new dashboard at a NEW layout. A held page from
    // the deleted one would make that a version CHANGE rather than a first page --
    // so the new page would go on probation against a ghost, the readiness window
    // would lapse, and the tab would show the DELETED layout under a kept-page
    // band saying the newer one did not load. With the page dropped there is no
    // previous page to keep, so the new one is shown at once and no band appears.
    const read = vi
      .spyOn(api, 'memberDashboard')
      .mockResolvedValue(page({ rendered_html: '<!doctype html><p>the deleted one</p>' }))
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-frame')

    read.mockResolvedValue({ state: 'empty', state_reason: 'package deleted', instance_version: 0 })
    await queryClient.refetchQueries({ queryKey: DASHBOARD_KEY })
    await screen.findByTestId('crew-dashboard-none')
    await flushEffects()

    read.mockResolvedValue(
      page({
        package: {
          slug: 'oncall-dashboard-2',
          version: 9,
          layout_fingerprint: 'sha256:new',
          bound_to: 'crewmate:oncall',
        },
        rendered_html: '<!doctype html><p>the newly composed one</p>',
      }),
    )
    await queryClient.refetchQueries({ queryKey: DASHBOARD_KEY })

    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('data-layout-version', '9')
    await flushEffects()
    // No probation against a ghost, so no band and no held predecessor.
    expect(screen.queryByTestId('crew-dashboard-kept-band')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-probe')).toBeNull()
    // And the document on screen is the new one, not the deleted layout.
    expect(mints.at(-1)).toContain('the newly composed one')
  }, 20000)

  it('still draws a STALE page, which is the crewmate\'s own and not a fallback', async () => {
    // The gate is on `empty`, not on "anything but live", and this is why. A stale
    // instance is a dashboard the crewmate adopted whose template shipped a new
    // version; refusing it would blank a page somebody chose in order to remove one
    // nobody did. The template registry is a live feature and this change does not
    // retire it.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue({
      state: 'stale',
      instance_version: 2,
      template: { id: 'project-report', version: 3 },
      rendered_html: '<!doctype html><p>adopted and stale</p>',
    })
    mount()
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
    expect(mints.some((m) => m.includes('adopted and stale'))).toBe(true)
  })

  it('and the stored copy is never mounted for any state', async () => {
    // THE CLIENT HALF of the finding the gateway's `_trusted_page` closes. `html` is
    // the crewmate's STORED page out of a writable record; `rendered_html` is what
    // the gateway composed. Falling back from the second to the first handed this
    // sandbox -- which grants scripts -- exactly the bytes the gateway had just
    // declined to run, so the server-side refusal bought nothing.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue({
      state: 'live',
      instance_version: 2,
      rendered_html: undefined,
      html: '<!doctype html><title>stored</title><script>stolen()</script>',
    })
    mount()
    await screen.findByTestId('crew-dashboard-empty')
    expect(mints).toEqual([])
    expect(screen.queryByTestId('crew-dashboard-frame')).toBeNull()
    expect(document.body.innerHTML).not.toContain('stolen()')
    expect(screen.queryByTestId('crew-dashboard-loading')).toBeNull()
  })
})
