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
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api, type DashboardManifest } from '../api/client'
import { ApiError } from '../api/apiError'
import { renderWithProviders } from './helpers'
import CrewDynamicDashboard, {
  ACT_MESSAGE_TYPE,
  DASHBOARD_FALLBACK_REFETCH_MS,
  DATA_MESSAGE_TYPE,
  READY_MESSAGE_TYPE,
} from '../pages/members/CrewDynamicDashboard'

/** A composed body carrying its read in the data island, as `compose_body` builds it. */
function withIsland(credits: number): string {
  return (
    `<script type="application/json" id="kirocrew-dashboard-data">${JSON.stringify({ fields: { credits } })}</script>` +
    `<p>${credits} credits</p>`
  )
}
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
    // BOTH halves, because a healthy read carries both. `html` is the crewmate's
    // stored copy and `rendered_html` is what the gateway composed from the
    // catalog's template for the id that record names -- and the composed one is
    // the ONLY one this component mounts, so a default carrying just `html` would
    // model a state the gateway never answers for a page it is willing to run.
    html: '<!doctype html><title>stored</title><p>hello</p>',
    rendered_html: '<!doctype html><title>report</title><p>hello</p>',
    manifest: MANIFEST,
    state: 'live' as const,
    ...over,
  }
}

function mount() {
  return renderWithProviders(
    <CrewDynamicDashboard target={{ kind: 'member', slug: 'oncall', member: 'oncall' }} displayName="On Call" />,
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

  it('reads the mate\'s own instance by slug AND exact member name', async () => {
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

  it('reads a root session\'s own page by its slot, through the same frame', async () => {
    const member = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    const session = vi.spyOn(api, 'sessionDashboard').mockResolvedValue(page())
    renderWithProviders(<CrewDynamicDashboard target={{ kind: 'session', slot: 'chat-7' }} displayName="Root" />)
    await waitFor(() => expect(session).toHaveBeenCalledWith('chat-7', expect.any(String), false))
    expect(member).not.toHaveBeenCalled()
    expect(await screen.findByTestId('crew-dashboard-iframe')).toHaveAttribute('sandbox', 'allow-scripts')
  })

  it('reads a root session\'s STAGED page when asked for the preview', async () => {
    const session = vi.spyOn(api, 'sessionDashboard').mockResolvedValue(page())
    renderWithProviders(<CrewDynamicDashboard target={{ kind: 'session', slot: 'chat-7' }} displayName="Root" preview />)
    await waitFor(() => expect(session).toHaveBeenCalledWith('chat-7', expect.any(String), true))
  })

  it('asks the session route for preview=1, and for nothing extra on a live read', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('null', { status: 200, headers: { 'content-type': 'application/json' } }))
    await api.sessionDashboard('chat-7', 'en', true)
    await api.sessionDashboard('chat-7')
    expect(fetchSpy.mock.calls.map(c => String(c[0]))).toEqual([
      '/api/chat/slots/chat-7/dashboard?locale=en&preview=1',
      '/api/chat/slots/chat-7/dashboard',
    ])
  })

  it('hands a session page\'s chosen reply to onAct, and only from its own frame', async () => {
    vi.spyOn(api, 'sessionDashboard').mockResolvedValue(page())
    const onAct = vi.fn()
    renderWithProviders(<CrewDynamicDashboard target={{ kind: 'session', slot: 'chat-7' }} displayName="Root" onAct={onAct} />)
    const frame = (await screen.findByTestId('crew-dashboard-iframe')) as HTMLIFrameElement
    // A message from some other window is ignored.
    window.dispatchEvent(new MessageEvent('message', { data: { type: ACT_MESSAGE_TYPE, text: 'nope' }, source: window }))
    expect(onAct).not.toHaveBeenCalled()
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: ACT_MESSAGE_TYPE, text: 'Re-dispatch it' }, source: frame.contentWindow,
    }))
    expect(onAct).toHaveBeenCalledWith('Re-dispatch it')
  })

  it('re-reads a session page when the gateway names its slot', async () => {
    const session = vi.spyOn(api, 'sessionDashboard').mockResolvedValue(page())
    const { queryClient } = renderWithProviders(<CrewDynamicDashboard target={{ kind: 'session', slot: 'chat-7' }} displayName="Root" />)
    await waitFor(() => expect(session).toHaveBeenCalledTimes(1))
    handleDashboardMoved(queryClient, { slot: 'chat-8' })
    handleDashboardMoved(queryClient, { slot: 'chat-7' })
    await waitFor(() => expect(session).toHaveBeenCalledTimes(2))
  })

  it('shows the page the read resolved, in a frame granting scripts and nothing else', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
  })

  it('mounts the gateway-composed page and never the stored copy', async () => {
    // Not a PREFERENCE between two pages: the stored copy is not a fallback at all.
    // It comes out of a writable `instance.json`, the composed one comes from the
    // catalog's own template, and this frame grants scripts -- so mounting the
    // stored copy when the gateway declined to compose would hand the sandbox
    // exactly the bytes the gateway had just refused.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      page({
        rendered_html: '<!doctype html><title>filled</title>',
        html: '<!doctype html><title>stored</title><script>stolen()</script>',
      }),
    )
    mount()
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
    expect(mints.at(-1)).toContain('filled')
    expect(mints.at(-1)).not.toContain('stolen()')
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
      .mockResolvedValueOnce(page({ rendered_html: withIsland(12) }))
      .mockResolvedValue(page({ rendered_html: withIsland(31) }))
    const { queryClient } = mount()
    await waitFor(() => expect(mints.some((m) => m.includes('12 credits'))).toBe(true))
    const frame = (await screen.findByTestId('crew-dashboard-iframe')) as HTMLIFrameElement
    const win = frame.contentWindow as Window
    const posted = vi.spyOn(win, 'postMessage')
    // The document says its page redraws on a refill (every builtin does).
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: READY_MESSAGE_TYPE, refill: true }, source: win }))
    })

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(1))
    // REFILLED IN PLACE, not reloaded: the open document gets the new read and no
    // second document is minted -- a new mint per read is the flicker.
    await waitFor(() =>
      expect(posted).toHaveBeenCalledWith(
        expect.objectContaining({ type: DATA_MESSAGE_TYPE, read: expect.objectContaining({ fields: { credits: 31 } }) }),
        '*',
      ),
    )
    expect(mints.some((m) => m.includes('31 credits'))).toBe(false)
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
      .mockResolvedValue(page({ instance_version: 2, rendered_html: '<!doctype html><p>v2</p>' }))
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
    rerender(<CrewDynamicDashboard target={{ kind: 'member', slug: 'oncall', member: 'oncall' }} displayName="On Call" />)
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

  it('tells a non-owner whose page it is, with no Retry', async () => {
    // The read refuses a caller who is not the owner on purpose; Retry reads the
    // same 403, so the tab must not draw it as a transient failure.
    vi.spyOn(api, 'memberDashboard').mockRejectedValue(
      new ApiError(403, 'owner authorization required', '{"code":"owner_only"}'),
    )
    mount()
    const notice = await screen.findByTestId('crew-dashboard-owner-only')
    expect(notice).toHaveTextContent("Only this mate's owner can see its dashboard.")
    expect(screen.queryByTestId('crew-dashboard-error')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-error-retry')).toBeNull()
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

  it('says the dashboard is unavailable when the read resolves no page at all', async () => {
    // An answer with no page and no `empty` state means the registry did not load --
    // which is what the copy names, rather than telling the reader to publish
    // something.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(null)
    mount()
    expect(await screen.findByTestId('crew-dashboard-empty')).toBeInTheDocument()
    expect(screen.getByTestId('crew-dashboard-empty-retry')).toBeInTheDocument()
  })

  it('reports nothing adopted as a STATE, with no retry', async () => {
    // Nothing adopted and nothing wrong are different answers. Until a built-in page
    // ships, `empty` is what EVERY crewmate's read answers, so rendering it as a
    // failure put a permanent error on every tab -- and offered a Try again that
    // re-reads the same empty answer forever, which reads as a fault the reader could
    // clear. Asserted together: the state's own line IS there, and neither the failure
    // notice nor its retry is.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      // The shape the route actually answers for this state: an empty body, not an
      // absent key. `instance._empty_instance` sets `html=""`, and the gateway
      // composes nothing for a state it will not render.
      page({
        state: 'empty',
        state_reason: 'no template adopted',
        html: '',
        rendered_html: undefined,
      }),
    )
    mount()
    expect(await screen.findByTestId('crew-dashboard-none')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-empty')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-empty-retry')).toBeNull()
  })

  // The read the gateway actually answers for a crewmate that adopted nothing:
  // `state: "empty"` with the DEFAULT template (project-report) composed in. The
  // tab shows the suggestions page instead of that default.
  const unadopted = () => page({
    state: 'empty',
    suggestions: true,
    state_reason: "no template adopted; showing the default 'project-report'",
    instance_version: 0,
    rendered_html: '<!doctype html><title>report</title><p>default report</p>',
  })

  it('shows the suggestions page, not the default template, for a mate with no adopted page', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(unadopted())
    mount()
    expect(await screen.findByTestId('crew-dashboard-suggestions')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-frame')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-loading')).toBeNull()
    expect(mints.some((m) => m.includes('default report'))).toBe(false)
  })

  it('puts a suggested prompt into the chat box through onAct', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(unadopted())
    const onAct = vi.fn()
    renderWithProviders(
      <CrewDynamicDashboard target={{ kind: 'member', slug: 'oncall', member: 'oncall' }} displayName="On Call" onAct={onAct} />,
    )
    const prompts = await screen.findAllByTestId('crew-dashboard-suggestion')
    expect(prompts).toHaveLength(3)
    fireEvent.click(prompts[0])
    expect(onAct).toHaveBeenCalledWith(expect.stringMatching(/dashboard/i))
  })

  it('withholds the prompts when there is no chat box to put them in', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(unadopted())
    mount()
    expect(await screen.findByTestId('crew-dashboard-suggestions')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-suggestions-prompts')).toBeNull()
  })

  it('keeps drawing an ADOPTED page, never the suggestions page', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ state: 'live' }))
    mount()
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-suggestions')).toBeNull()
  })

  it('draws the page it was sent when the gateway does not say suggestions', async () => {
    // The shipped page named for the crewmate (`kirocrew-conductor` gets its board)
    // reads `empty` without the flag: the gateway decides, the tab does not re-derive.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      page({ ...unadopted(), suggestions: false, template: { id: 'kirocrew-conductor', version: 1 } }),
    )
    renderWithProviders(
      <CrewDynamicDashboard target={{ kind: 'member', slug: 'kirocrew-conductor', member: 'kirocrew-conductor' }} displayName="Conductor" />,
    )
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-suggestions')).toBeNull()
  })

  it('a bound package with nothing applied keeps its own empty state', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      page({ state: 'empty', html: '', rendered_html: undefined, dashboard: 'my-pkg' }),
    )
    mount()
    expect(await screen.findByTestId('crew-dashboard-none')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-suggestions')).toBeNull()
  })

  it('a session target with nothing adopted still draws the default page', async () => {
    vi.spyOn(api, 'sessionDashboard').mockResolvedValue(unadopted() as never)
    renderWithProviders(<CrewDynamicDashboard target={{ kind: 'session', slot: 'chat-7' }} displayName="Root" />)
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-suggestions')).toBeNull()
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
  describe('live refresh', () => {
    it('re-renders the tab when a mate writes a value, with no reload', async () => {
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

    it('re-reads when a fold advances, not only on a mate write', async () => {
      // A `member_projection` frame IS a fold advance, and every number on the page
      // but the crewmate's own writes comes from a fold. A tab live only for writes
      // would sit on stale costs and counts for the whole of a long turn.
      const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
      const { queryClient } = mount()
      await waitFor(() => expect(read).toHaveBeenCalled())
      const before = read.mock.calls.length

      handleDashboardMoved(queryClient, { slug: 'oncall', key: 'workstreams', seq: 12 })

      await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(before))
    })

    it('leaves another mate\'s open tab alone', async () => {
      // The frame is slug-keyed, so a write to one crewmate must not cost every other
      // open dashboard a read. Without the slug in the key this passes anyway and the
      // cost only shows up on a roster.
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
})


describe('the stored copy is never mounted', () => {
  // THE CLIENT HALF of the finding the gateway's `_trusted_page` closes.
  //
  // `data.html` is the crewmate's STORED page, out of a writable `instance.json`;
  // `data.rendered_html` is what the gateway composed from the catalog's own
  // template for the id that record names. Falling back from the second to the
  // first handed this sandbox -- which grants scripts -- exactly the bytes the
  // gateway had just declined to run, so the server-side refusal bought nothing.
  beforeEach(() => {
    vi.restoreAllMocks()
    mints.length = 0
    retries = 0
    retrySpy.mockClear()
  })

  it('a healthy read the gateway would not compose mounts no page', async () => {
    // `state: live` on purpose. This is not the broken-manifest case, which has
    // its own refusal: this is a record that LOOKS healthy and whose page the
    // gateway refused to compose -- a tampered one, or an id the catalog does not
    // serve.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      page({
        state: 'live',
        rendered_html: undefined,
        html: '<!doctype html><title>stored</title><script>stolen()</script>',
      }),
    )
    mount()
    // SETTLE FIRST, on a positive signal. `waitFor` resolves the moment its callback
    // stops throwing, so waiting for the frame to be ABSENT succeeds on the first
    // poll -- before the read has even landed -- and the case passes whatever the
    // component goes on to do. Waiting for the unavailable state is waiting for the
    // read to have arrived and the component to have decided.
    await screen.findByTestId('crew-dashboard-empty')
    // Now the absence means something: nothing was minted, so nothing is mounted.
    // The frame is built from a minted document, and `mints` is every document this
    // component asked for.
    expect(mints).toEqual([])
    expect(screen.queryByTestId('crew-dashboard-frame')).toBeNull()
    expect(document.body.innerHTML).not.toContain('stolen()')
  })

  it('and it does not wait forever for a page that is not coming', async () => {
    // The other half: refusing to mount must not leave the skeleton up for good.
    // A reader has to be told the page is unavailable.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      page({ state: 'live', rendered_html: undefined }),
    )
    mount()
    expect(await screen.findByTestId('crew-dashboard-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-loading')).toBeNull()
  })
})

/* The person's structure lock. The tab only SHOWS it, as a small read-only badge:
 * the switch that sets it is on the crewmate's Profile card
 * (CrewProfileSettings.test.tsx), so nothing here is clickable. */
describe('the lock badge', () => {
  it('draws a small Locked badge, and no control, when the gateway body says locked', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ structure_locked: true }))
    mount()
    const badge = await screen.findByTestId('crew-dashboard-lock-badge')
    expect(badge).toHaveTextContent('Layout locked — unlock on Profile')
    expect(badge).toHaveAccessibleName("Dashboard layout locked. Unlock it on the Profile tab.")
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByRole('button', { name: /lock/i })).toBeNull()
  })

  it('draws nothing about the lock while the page is unlocked', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    await screen.findByTestId('crew-dashboard-frame')
    expect(screen.queryByTestId('crew-dashboard-lock-badge')).toBeNull()
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it.each([
    ['broken', page({ state: 'error', rendered_html: undefined, structure_locked: true }), 'crew-dashboard-broken'],
    ['empty', page({ state: 'empty', html: '', rendered_html: undefined, structure_locked: true }), 'crew-dashboard-none'],
    ['unrenderable', page({ state: 'stale', rendered_html: undefined, structure_locked: true }), 'crew-dashboard-empty'],
  ])('keeps the badge on a locked %s view, so the person can see why nothing changes', async (_name, body, testId) => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(body)
    mount()
    await screen.findByTestId(testId)
    expect(screen.getByTestId('crew-dashboard-lock-badge')).toHaveTextContent('Layout locked — unlock on Profile')
  })

  it('draws no badge over a staged preview, which is not the page the lock holds', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ structure_locked: true }))
    renderWithProviders(
      <CrewDynamicDashboard target={{ kind: 'member', slug: 'oncall', member: 'oncall' }} displayName="On Call" preview />,
    )
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-lock-badge')).toBeNull()
  })

  it('reads a session page as never locked, whatever its body carries', async () => {
    vi.spyOn(api, 'sessionDashboard').mockResolvedValue(page({ structure_locked: true }))
    renderWithProviders(<CrewDynamicDashboard target={{ kind: 'session', slot: 'chat-7' }} displayName="Root" />)
    expect(await screen.findByTestId('crew-dashboard-frame')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-lock-badge')).toBeNull()
  })
})

describe('a preview link: the STAGED page, labelled as one', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    mints.length = 0
    retries = 0
    retrySpy.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  function mountPreview(onPreviewGone?: (gone: boolean) => void) {
    return renderWithProviders(
      <CrewDynamicDashboard
        target={{ kind: 'member', slug: 'oncall', member: 'oncall' }}
        displayName="On Call"
        preview
        onPreviewGone={onPreviewGone}
      />,
    )
  }

  it('reads the staged page, not the live record', async () => {
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ preview: true }))
    mountPreview()
    await waitFor(() => expect(read).toHaveBeenCalledWith('oncall', 'oncall', 'en', true))
    expect(read).not.toHaveBeenCalledWith('oncall', 'oncall', 'en')
  })

  it('draws it in the same sandbox and leaves the label to its host', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(
      page({ preview: true, rendered_html: '<!doctype html><title>staged</title>' }),
    )
    mountPreview()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
    expect(mints[mints.length - 1]).toContain('<title>staged</title>')
    expect(screen.getByTestId('crew-dashboard-frame')).toHaveAttribute('data-preview', 'true')
    expect(screen.queryByTestId('dashboard-preview-badge')).toBeNull()
  })

  it('says plainly when nothing is staged any more, with no retry, and tells the host', async () => {
    const read = vi
      .spyOn(api, 'memberDashboard')
      .mockRejectedValue(new ApiError(404, 'nothing is staged to preview', '{"code":"no_preview"}'))
    mountPreview()
    expect(await screen.findByTestId('crew-dashboard-preview-gone')).toHaveTextContent('This preview has closed.')
    expect(screen.queryByTestId('crew-dashboard-error')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-error-retry')).toBeNull()
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('tells a listening host and draws nothing, so the host can hand over with no flash', async () => {
    vi.spyOn(api, 'memberDashboard')
      .mockRejectedValue(new ApiError(404, 'nothing is staged to preview', '{"code":"no_preview"}'))
    const onGone = vi.fn()
    mountPreview(onGone)
    await waitFor(() => expect(onGone).toHaveBeenLastCalledWith(true))
    expect(screen.queryByTestId('crew-dashboard-preview-gone')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-error')).toBeNull()
  })

  it('any other 404 is a load failure, not a preview that ended', async () => {
    // `member_not_found` comes off the same route for a crewmate that is gone. It
    // keeps the client's ladder: one retry, one second later, then the error.
    vi.useFakeTimers()
    const read = vi.spyOn(api, 'memberDashboard').mockRejectedValue(
      new ApiError(404, 'no crew member for this slug', '{"code":"member_not_found"}'),
    )
    mountPreview()
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(read).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('crew-dashboard-error')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(read).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('crew-dashboard-error')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-preview-gone')).toBeNull()
  })

  it('the live page is not marked as a preview', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    await screen.findByTestId('crew-dashboard-iframe')
    expect(screen.getByTestId('crew-dashboard-frame')).not.toHaveAttribute('data-preview')
  })
})
