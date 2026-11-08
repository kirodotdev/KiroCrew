// @vitest-environment jsdom
/**
 * Dashboard edit mode over a Bot's sandboxed page: the host turns Edit on in the frame,
 * refuses a path the page does not declare, draws the gateway's lineage for one it does,
 * and sends a change request only from the person's own Send.
 */
import { useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api, type DashboardLineage } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { renderWithProviders } from '../../test/helpers'
import CrewDashboardEditLayer from './CrewDashboardEditLayer'
import { MEMBER_DASHBOARD_QUERY_PREFIX } from '../../hooks/useWebSocket'
import { fmtDateTime } from '../../i18n/format'

/** The field-push listeners the layer subscribes, so a case can deliver a push. */
const pushes = vi.hoisted(() => [] as ((read: Record<string, unknown>, dashboard: string) => void)[])
vi.mock('../../hooks/useWebSocket', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../hooks/useWebSocket')>()),
  subscribeDashboardFields: (_slug: string, fn: (read: Record<string, unknown>, dashboard: string) => void) => {
    pushes.push(fn)
    return () => { pushes.splice(pushes.indexOf(fn), 1) }
  },
}))
import { ACTIVATE_MESSAGE_TYPE, EDIT_KEY_MESSAGE_TYPE, EDIT_MESSAGE_TYPE, HOVER_DWELL_MS, HOVER_LEAVE_MS, PICKED_MESSAGE_TYPE, holdDuration } from './dashboardEdit'

const MANIFEST = {
  id: 'project-report', version: 1, title: '', description: '', source: 'builtin',
  fields: {
    accepted: { type: 'number', source: { fold: 'workstreams', path: 'accepted' } },
    verdict: { type: 'string', source: { agentic: true } },
  },
}

const LINEAGE: DashboardLineage = {
  path: 'accepted', field: 'accepted', kind: 'fold', type: 'number', found: true, value: 7,
  hops: [
    { kind: 'field', field: 'accepted', type: 'number', fold: 'workstreams', path: 'accepted' },
    { kind: 'fold', fold: 'workstreams', path: 'accepted', found: true, by: 'gateway' },
    { kind: 'crew_log', fold: 'workstreams', needs: 'entry seq on fold rows' },
  ],
  edit: { kind: 'fold' },
}

function Host({ onSend, version = 14, page = 'template:project-report', hidden = false, collapsed = false, paused = false, drafts, mounted = true }: { onSend?: (text: string) => Promise<boolean>; version?: number; page?: string; hidden?: boolean; collapsed?: boolean; paused?: boolean; drafts?: Map<string, unknown>; mounted?: boolean }) {
  const ref = useRef<HTMLIFrameElement | null>(null)
  return (
    // `collapsed` hides the way the side panel does: an inline display:none.
    <div className="relative" hidden={hidden} style={collapsed ? { display: 'none' } : undefined}>
      <iframe ref={ref} title="page" data-testid="frame" />
      {mounted && <CrewDashboardEditLayer
        frameRef={ref}
        frameKey="k"
        slug="atlas"
        member="Atlas"
        botName="Atlas"
        manifest={MANIFEST}
        board="project-report"
        page={page}
        instanceVersion={version}
        onSend={onSend}
        onDone={() => {}}
        paused={paused}
        drafts={drafts}
      />}
    </div>
  )
}

/** The Dashboard's own order: the layer renders BEFORE a keyed frame, so a new key
 *  replaces the iframe after the layer's own commit work. */
function LayerFirstHost({ frameKey }: { frameKey: string }) {
  const ref = useRef<HTMLIFrameElement | null>(null)
  return (
    <div className="relative">
      <CrewDashboardEditLayer frameRef={ref} frameKey={frameKey} slug="atlas" member="Atlas" botName="Atlas"
        manifest={MANIFEST} board="project-report" page="template:project-report" instanceVersion={14} onDone={() => {}} />
      <iframe key={frameKey} ref={ref} title="page" data-testid="frame" />
    </div>
  )
}

function frameWindow(): Window {
  return (screen.getByTestId('frame') as HTMLIFrameElement).contentWindow as Window
}

/** The frame's answer for keyboard focus (`seq: -1`). The hover dwell is waited out
 *  by each case's `findBy*`, which polls. */
function pick(data: Record<string, unknown>) {
  act(() => {
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: PICKED_MESSAGE_TYPE, seq: -1, rect: { x: 10, y: 10, w: 30, h: 20 }, block: '', blockRect: null, blockFields: [], ...data },
      source: frameWindow(),
    }))
  })
}

/** Tap the capture layer and answer the point it asks for, as the frame would. */
function tap(answer: Record<string, unknown>) {
  const posted: { type?: string; seq?: number }[] = []
  const spy = vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m as { seq?: number }) })
  fireEvent.click(screen.getByTestId('dashboard-edit-capture'), { clientX: 20, clientY: 20 })
  spy.mockRestore()
  const point = posted.find(m => m.type === 'kirocrew-dashboard:point')
  expect(point?.seq).toBeGreaterThan(0)
  pick({ seq: point?.seq, ...answer })
}

afterEach(() => vi.restoreAllMocks())

describe('CrewDashboardEditLayer', () => {
  it('turns Edit on in the frame it draws over', async () => {
    const posted: unknown[] = []
    renderWithProviders(<Host />)
    vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m) })
    act(() => { window.dispatchEvent(new MessageEvent('message', { data: { type: 'kirocrew-dashboard:ready' }, source: frameWindow() })) })
    expect(posted).toContainEqual({ type: EDIT_MESSAGE_TYPE, on: true })
  })

  it('refuses a path the page does not declare and asks the gateway nothing', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage')
    renderWithProviders(<Host />)
    pick({ path: 'secret' })
    const refused = await screen.findByTestId('dashboard-edit-refused')
    expect(refused).toHaveTextContent('secret')
    // A hint from the host's own check, not a failed request: no alert is raised.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(lineage).not.toHaveBeenCalled()
  })

  it('draws the lineage the gateway built for a declared path', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    expect(await screen.findByTestId('lineage-popover')).toBeInTheDocument()
    expect(lineage).toHaveBeenCalledWith('atlas', 'Atlas', 'accepted')
    expect(await screen.findByTestId('lineage-value')).toHaveTextContent('7')
    expect(screen.getByTestId('lineage-hop-crew_log').querySelector('[title="entry seq on fold rows"]')).not.toBeNull()
    // The actions are pinned to the popover's bottom, so a long lineage never scrolls
    // them out of view.
    expect(screen.getByTestId('lineage-edit')).toHaveClass('sticky')
    // A folded value: Edit offers "change how it's shown"; a hand correction is greyed.
    fireEvent.click(screen.getByTestId('lineage-edit-open'))
    expect(screen.getByTestId('lineage-ask')).toBeInTheDocument()
    expect(screen.getByTestId('lineage-correct-greyed')).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByTestId('lineage-correct')).toBeNull()
  })

  it('pins the popover while Edit is open, and Esc closes Edit before the popover', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-value')
    fireEvent.keyDown(window, { key: 'e' })
    expect(await screen.findByTestId('lineage-edit-menu')).toBeInTheDocument()
    // Another value under the pointer does not replace a pinned popover.
    pick({ path: 'verdict' })
    pick({ path: null, rect: null })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(screen.getByTestId('lineage-edit-menu')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('lineage-edit-menu')).toBeNull()
    expect(screen.getByTestId('lineage-popover')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('lineage-popover')).toBeNull()
  })

  it('says every hop time as a local date, never the raw ISO', async () => {
    const at = '2026-11-11T10:00:00+00:00'
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      hops: [
        { kind: 'write', actor: 'person', at, found: true },
        { kind: 'earlier_write', actor: 'bot', at, found: true },
        { kind: 'record', writer: 'cron:p99', at, found: true },
        { kind: 'script', job: 'p99', script: 'digest.py:run', run_at: at },
        { kind: 'run', failed_at: at, last_error: 'boom' },
      ],
    } as DashboardLineage)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-hop-write')
    for (const kind of ['write', 'earlier_write', 'record', 'script']) {
      const hop = screen.getByTestId(`lineage-hop-${kind}`)
      expect(hop.textContent).not.toContain('2026-11-11T')
      expect(hop).toHaveTextContent(fmtDateTime(at))
    }
    const run = screen.getByTestId('lineage-hop-run')
    expect(run.innerHTML).not.toContain('2026-11-11T')
  })

  it("asks before it runs a script's job", async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [{ kind: 'freshness', max_age_s: 600, stale_after: '', by: 'script' }],
      edit: { kind: 'source', writer: 'cron:p99', job: 'p99' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [] })
    const run = vi.spyOn(api, 'runCron').mockResolvedValue({ ok: true })
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-source'))
    // The view is named for what the button promised: a look at the job, not an editor.
    expect(screen.getByTestId('lineage-job-heading')).toHaveTextContent('The job behind this value')
    // The hold reads as a duration, never as a bare count of seconds.
    expect(screen.getByTestId('lineage-source-box')).toHaveTextContent(`Counts as current for${holdDuration(600)}`)
    expect(screen.getByTestId('lineage-source-box')).not.toHaveTextContent('600')
    fireEvent.click(await screen.findByTestId('lineage-run'))
    expect(run).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('lineage-run-yes'))
    await waitFor(() => expect(run).toHaveBeenCalledWith('p99'))
  })

  it('starts one run for a double-clicked confirm and keeps saying it started', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [{ kind: 'freshness', max_age_s: 600, stale_after: '', by: 'script' }],
      edit: { kind: 'source', writer: 'cron:p99', job: 'p99' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [] })
    let finish: (v: unknown) => void = () => {}
    const run = vi.spyOn(api, 'runCron')
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
      .mockRejectedValue(new Error('409 already running'))
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-source'))
    fireEvent.click(await screen.findByTestId('lineage-run'))
    const yes = screen.getByTestId('lineage-run-yes')
    fireEvent.click(yes)
    fireEvent.click(yes)
    await act(async () => { finish({ ok: true }); await Promise.resolve() })
    expect(run).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/Run started/)).toBeInTheDocument()
    expect(screen.queryByText('The run could not start.')).toBeNull()
    // A started run is not offered again from this box.
    expect(screen.getByTestId('lineage-run')).toBeDisabled()
  })

  it('drops an open run confirmation when a refresh names another job', async () => {
    const source = (job: string) => ({
      ...LINEAGE,
      kind: 'custom' as const,
      hops: [{ kind: 'freshness' as const, max_age_s: 600, stale_after: '', by: 'script' }],
      edit: { kind: 'source' as const, writer: `cron:${job}`, job },
    })
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(source('p99'))
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [] })
    const run = vi.spyOn(api, 'runCron').mockResolvedValue({ ok: true })
    const { queryClient } = renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-source'))
    fireEvent.click(await screen.findByTestId('lineage-run'))
    expect(screen.getByTestId('lineage-run-confirm')).toBeInTheDocument()
    // Another job now writes this record; the open lineage re-reads.
    lineage.mockResolvedValue(source('other'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas', 'Atlas', 'lineage', 'accepted'] }) })
    await waitFor(() => expect(screen.getByTestId('lineage-source-box')).toHaveTextContent('other'))
    // The confirm was given for p99, so it never carries over to the other job.
    expect(screen.queryByTestId('lineage-run-confirm')).toBeNull()
    expect(run).not.toHaveBeenCalled()
  })

  it('opens a tapped component when the pointer moves before its answer arrives', async () => {
    renderWithProviders(<Host />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { cb(0); return 0 })
    const posted: { type?: string; seq?: number }[] = []
    vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m as { seq?: number }) })
    const capture = screen.getByTestId('dashboard-edit-capture')
    fireEvent.click(capture, { clientX: 20, clientY: 20 })
    const tapSeq = posted.filter(m => m.type === 'kirocrew-dashboard:point').at(-1)?.seq
    // The pointer moves on: a newer hover request goes out before the tap's answer.
    fireEvent.pointerMove(capture, { pointerType: 'mouse', clientX: 40, clientY: 40 })
    expect(posted.filter(m => m.type === 'kirocrew-dashboard:point').at(-1)?.seq).toBeGreaterThan(tapSeq ?? 0)
    pick({ seq: tapSeq, path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    expect(await screen.findByTestId('dashboard-edit-flip')).toHaveTextContent('Change tasks')
  })

  it('flips a component and sends the request only on the person\'s Send', async () => {
    const onSend = vi.fn().mockResolvedValue('sent')
    renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted', 'verdict', 'undeclared'] })
    const provider = await screen.findByTestId('dashboard-edit-provider')
    // Titled by what the card does: a change request, not a look at where data comes from.
    expect(screen.getByTestId('dashboard-edit-flip')).toHaveTextContent('Change tasks')
    expect(provider).toHaveTextContent('accepted')
    expect(provider).toHaveTextContent('verdict')
    expect(provider).not.toHaveTextContent('undeclared')
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
    expect(onSend.mock.calls[0][0]).toBe('show CI state\n\n[Dashboard change request · board project-report · component tasks · field accepted · page v14]')
  })

  it('keeps naming the page version a request was opened on when a new version lands', async () => {
    const onSend = vi.fn().mockResolvedValue('sent')
    const { rerender } = renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    await screen.findByTestId('dashboard-edit-provider')
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    // The Bot applies v15 while the draft is open; the layer stays mounted.
    rerender(<Host onSend={onSend} version={15} />)
    expect(screen.getByTestId('dashboard-edit-flip')).toHaveTextContent('v14')
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
    expect(onSend.mock.calls[0][0]).toContain('· page v14]')
  })

  it('turns the card over from the component it replaces, then moves it into place', async () => {
    // jsdom lays nothing out: give the pane and the card a size.
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(800)
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(600)
    const frames: FrameRequestCallback[] = []
    renderWithProviders(<Host onSend={vi.fn()} />)
    // Held frames from here: the card mounts inside the tap's answer.
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length })
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 40, y: 60, w: 680, h: 200 }, blockFields: ['accepted'] })
    await screen.findByTestId('dashboard-edit-flip')
    const shell = screen.getByTestId('flip-card-shell')
    // The first frame sits on the component's box, scaled to its size.
    await waitFor(() => expect(shell.style.transform).toMatch(/^translate\(0px, 0px\) scale\(/))
    expect(shell.style.transition).toBe('none')
    let ran = 0
    act(() => { while (frames.length) { frames.shift()?.(0); ran++ } })
    expect(ran).toBeGreaterThan(1)
    expect(shell.style.transform).toBe('none')
    expect(shell.style.transition).toMatch(/^transform 260ms/)
  })

  it('swaps to the card at once under reduced motion', async () => {
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(800)
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(600)
    vi.spyOn(window, 'matchMedia').mockImplementation((q: string) => ({
      matches: q.includes('reduce'), media: q, onchange: null,
      addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
    }) as MediaQueryList)
    renderWithProviders(<Host onSend={vi.fn()} />)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 40, y: 60, w: 680, h: 200 }, blockFields: ['accepted'] })
    const card = await screen.findByTestId('dashboard-edit-flip')
    const shell = screen.getByTestId('flip-card-shell')
    expect(shell.style.transform).toBe('none')
    expect(shell.style.transition).toBe('none')
    expect(card.style.transform).toBe('rotateY(0deg)')
  })

  it('flips the focused component on Enter, the keyboard path to the same card', async () => {
    renderWithProviders(<Host />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    pick({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: ACTIVATE_MESSAGE_TYPE }, source: frameWindow() }))
    })
    expect(await screen.findByTestId('dashboard-edit-provider')).toHaveTextContent('accepted')
  })

  it('moves the focus into an editor a frame key opened, and back to the frame on Esc', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-edit-open')
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: EDIT_KEY_MESSAGE_TYPE }, source: frameWindow() }))
    })
    // Edit opened from the frame holds the focus in the host, on its first option.
    await waitFor(() => expect(screen.getByTestId('lineage-edit')).toContainElement(document.activeElement as HTMLElement))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('lineage-ask')).toBeNull()
    expect(document.activeElement).toBe(screen.getByTestId('frame'))
  })

  it('focuses the request box of a component the frame opened with Enter', async () => {
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: ACTIVATE_MESSAGE_TYPE }, source: frameWindow() }))
    })
    const box = await screen.findByTestId('dashboard-edit-comment')
    expect(document.activeElement).toBe(box)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('dashboard-edit-flip')).toBeNull()
    expect(document.activeElement).toBe(screen.getByTestId('frame'))
  })

  it('leaves an "e" typed into the chat composer alone', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-edit-open')
    const composer = document.createElement('div')
    composer.contentEditable = 'true'
    // jsdom does not compute isContentEditable from the attribute.
    Object.defineProperty(composer, 'isContentEditable', { value: true })
    document.body.appendChild(composer)
    const typed = new KeyboardEvent('keydown', { key: 'e', bubbles: true, cancelable: true })
    act(() => { composer.dispatchEvent(typed) })
    expect(typed.defaultPrevented).toBe(false)
    expect(screen.queryByTestId('lineage-edit-menu')).toBeNull()
    composer.remove()
  })

  it('keeps the popover open when the frame\'s "nothing here" arrives after the pointer entered it', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    const popover = await screen.findByTestId('lineage-popover')
    vi.useFakeTimers()
    try {
      fireEvent.mouseEnter(popover)
      // The late reply to the pointer leaving the value for the popover.
      pick({ path: null, rect: null })
      act(() => { vi.advanceTimersByTime(HOVER_LEAVE_MS + 1) })
      expect(screen.getByTestId('lineage-popover')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-reads an open lineage when the page\'s values are pushed', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-value')
    expect(lineage).toHaveBeenCalledTimes(1)
    expect(pushes.length).toBeGreaterThan(0)
    act(() => { for (const fn of [...pushes]) fn({ fields: { accepted: 8 } }, '') })
    await waitFor(() => expect(lineage).toHaveBeenCalledTimes(2))
  })

  it('never stacks lineage reads: pushes during a read owe exactly one more', async () => {
    let finish: (v: DashboardLineage) => void = () => {}
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValueOnce(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-value')
    lineage.mockImplementationOnce(() => new Promise<DashboardLineage>((resolve) => { finish = resolve }))
    lineage.mockResolvedValue(LINEAGE)
    const push = () => act(() => { for (const fn of [...pushes]) fn({ fields: { accepted: 8 } }, '') })
    push()
    await waitFor(() => expect(lineage).toHaveBeenCalledTimes(2))
    // Five more frames while that read is out: none starts a read of its own.
    for (let i = 0; i < 5; i++) push()
    await act(async () => { await Promise.resolve() })
    expect(lineage).toHaveBeenCalledTimes(2)
    // Once it settles, the frames are owed exactly one newer read.
    await act(async () => { finish(LINEAGE); await Promise.resolve() })
    await waitFor(() => expect(lineage).toHaveBeenCalledTimes(3))
    // The owed read is the only one: settling it owes nothing more.
    await act(async () => { await Promise.resolve() })
    expect(lineage).toHaveBeenCalledTimes(3)
  })

  it("says a script job's failed last run through the shared error notice", async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [],
      edit: { kind: 'source', writer: 'cron:p99', job: 'p99' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [{ id: 'p99', name: 'p99', last_error: 'throttled' }] } as never)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-source'))
    expect(await screen.findByTestId('lineage-job-failed')).toHaveTextContent('throttled')
    // No hand-off: it would navigate away from the Members page's unsent DM draft.
    expect(within(screen.getByTestId('lineage-job-failed')).queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('opens what is under a tap that had no hover before it, not a stale pick', async () => {
    renderWithProviders(<Host onSend={vi.fn()} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    // No pointer move: the only pick is the tap's own answer.
    tap({ path: 'verdict', block: 'status', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['verdict'] })
    const provider = await screen.findByTestId('dashboard-edit-provider')
    expect(provider).toHaveTextContent('verdict')
    expect(provider).not.toHaveTextContent('accepted')
  })

  it('keeps an unsent draft when the dashboard unmounts and remounts the layer', async () => {
    // A Bot applying a package clears the page and shows the loading skeleton, which
    // unmounts the layer. Reddens if the drafts go back to the layer's own state.
    const drafts = new Map<string, unknown>()
    const onSend = vi.fn().mockResolvedValue('sent')
    const { rerender } = renderWithProviders(<Host onSend={onSend} drafts={drafts} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'keep me' } })
    rerender(<Host onSend={onSend} drafts={drafts} mounted={false} />)
    expect(screen.queryByTestId('dashboard-edit-comment')).toBeNull()
    rerender(<Host onSend={onSend} drafts={drafts} version={15} />)
    expect(await screen.findByTestId('dashboard-edit-comment')).toHaveValue('keep me')
    // Still the page it was typed on.
    expect(screen.getByTestId('dashboard-edit-flip')).toHaveTextContent('v14')
  })

  it('drops a draft its person closed, so a remount opens nothing', async () => {
    const drafts = new Map<string, unknown>()
    const { rerender } = renderWithProviders(<Host drafts={drafts} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'gone' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-flip-cancel'))
    await waitFor(() => expect(screen.queryByTestId('dashboard-edit-comment')).toBeNull())
    rerender(<Host drafts={drafts} mounted={false} />)
    rerender(<Host drafts={drafts} />)
    expect(screen.queryByTestId('dashboard-edit-flip')).toBeNull()
    expect(drafts.get('flip')).toBeNull()
    expect([...drafts.keys()].filter(k => k.startsWith('flip\u0000'))).toEqual([])
  })

  it('reads a send still out at unmount as unconfirmed, so it is not sent twice', async () => {
    const drafts = new Map<string, unknown>()
    const onSend = vi.fn().mockImplementation(() => new Promise(() => {}))
    const { rerender } = renderWithProviders(<Host onSend={onSend} drafts={drafts} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'once' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    rerender(<Host onSend={onSend} drafts={drafts} mounted={false} />)
    rerender(<Host onSend={onSend} drafts={drafts} />)
    expect(await screen.findByTestId('dashboard-edit-send-unconfirmed')).toBeTruthy()
    expect(screen.getByTestId('dashboard-edit-send')).toBeDisabled()
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('keeps a reopened draft when the closed one\'s send answers late', async () => {
    // A late completion of a closed draft must not write into the draft opened after
    // it on the same field, even across a remount. Reddens if a late write is fenced
    // by key presence alone.
    const drafts = new Map<string, unknown>()
    let finish: (outcome: 'sent') => void = () => {}
    const onSend = vi.fn()
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockResolvedValue('refused')
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    const { rerender } = renderWithProviders(<Host onSend={onSend} drafts={drafts} />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'first' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    // Escape closes Edit while the send is out; Edit is opened again on the same field.
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('dashboard-edit-comment')).toBeNull())
    fireEvent.click(screen.getByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'second' } })
    // The reopened draft's own send is refused, so its state is written as failed.
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    expect(await screen.findByTestId('dashboard-edit-send-failed')).toBeTruthy()
    await act(async () => { finish('sent'); await Promise.resolve() })
    rerender(<Host onSend={onSend} drafts={drafts} mounted={false} />)
    rerender(<Host onSend={onSend} drafts={drafts} />)
    expect(await screen.findByTestId('dashboard-edit-comment')).toHaveValue('second')
    // Still the reopened draft's own outcome, not the closed draft's late "sent".
    expect(screen.getByTestId('dashboard-edit-send-failed')).toBeTruthy()
    expect(screen.queryByTestId('dashboard-edit-sent-done')).toBeNull()
  })

  it("keeps another component's draft when an earlier send completes after its card closed", async () => {
    let finish: (outcome: 'sent') => void = () => {}
    const onSend = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    // Component A: send, then close its card while the send is still out.
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'first' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('dashboard-edit-comment')).toBeNull())
    // Component B: a new draft.
    tap({ path: 'verdict', block: 'status', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['verdict'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'second draft' } })
    vi.useFakeTimers()
    try {
      await act(async () => { finish('sent'); await Promise.resolve() })
      act(() => { vi.advanceTimersByTime(2000) })
      expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('second draft')
    } finally {
      vi.useRealTimers()
    }
  })

  it('says a gateway refusal of a correction through the error notice, not the type hint', async () => {
    vi.spyOn(api, 'memberDashboardCorrect').mockRejectedValue(new ApiError(400, 'wrong type', JSON.stringify({ code: 'wrong_type', error: 'wrong type' })))
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    fireEvent.change(screen.getByLabelText(/value/i), { target: { value: 'green' } })
    await act(async () => { fireEvent.click(screen.getByTestId('dashboard-edit-correct-save')) })
    expect(await screen.findByTestId('dashboard-edit-correct-failed')).toBeInTheDocument()
    expect(screen.getByTestId('dashboard-edit-correct-hint')).toHaveTextContent('')
    expect(screen.getByLabelText(/value/i)).toHaveValue('green')
  })

  it('keeps a correction draft when a refresh rebinds the field, with Save off', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    const { queryClient } = renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    const box = screen.getByTestId('dashboard-edit-correct')
    fireEvent.change(box.querySelectorAll('input')[0], { target: { value: 'green' } })
    // The Bot applies a page that makes the field counted; the open lineage re-reads.
    lineage.mockResolvedValue({ ...LINEAGE, path: 'verdict', field: 'verdict' })
    await act(async () => { await queryClient.refetchQueries({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas', 'Atlas', 'lineage', 'verdict'] }) })
    await waitFor(() => expect(screen.getByTestId('dashboard-edit-correct-save')).toBeDisabled())
    expect(screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')[0]).toHaveValue('green')
    expect(screen.getByTestId('dashboard-edit-correct-hint')).toHaveTextContent(/counted from/i)
  })

  it('labels both correction boxes and says what Save does', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    const box = screen.getByTestId('dashboard-edit-correct')
    // Visible labels, so a typed value still says which box it is.
    const labels = Array.from(box.querySelectorAll('label')).map(l => l.textContent)
    expect(labels).toEqual(['New value', 'Why (optional)'])
    const [value, why] = Array.from(box.querySelectorAll('input'))
    expect(screen.getByLabelText('New value')).toBe(value)
    expect(screen.getByLabelText('Why (optional)')).toBe(why)
    expect(screen.getByTestId('dashboard-edit-correct-effect')).toHaveTextContent('Atlas is told on its next turn')
  })

  it('saves an empty correction to clear a string value', async () => {
    const correct = vi.spyOn(api, 'memberDashboardCorrect').mockResolvedValue({ ok: true } as never)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    // The shared Input, so both fields carry the app's focus ring.
    for (const field of screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')) expect(field).toHaveClass('focus-ring')
    const save = screen.getByTestId('dashboard-edit-correct-save')
    expect(save).toBeEnabled()
    await act(async () => { fireEvent.click(save) })
    // The page version the person typed against rides along, so a newer page refuses it.
    expect(correct).toHaveBeenCalledWith('atlas', 'Atlas', 'verdict', '', '', { page: 'template:project-report', version: 14 })
  })

  it('asks to confirm an open correction when another page at the same version replaces it', async () => {
    const correct = vi.spyOn(api, 'memberDashboardCorrect').mockResolvedValue({ ok: true } as never)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    const { rerender } = renderWithProviders(<Host onSend={vi.fn()} version={1} page="template:project-report" />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    fireEvent.change(screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')[0], { target: { value: 'green' } })
    // The first package apply: package version 1 over template counter 1. Same number,
    // another page, so the draft waits for a confirm.
    rerender(<Host onSend={vi.fn()} version={1} page="package:report" />)
    expect(screen.getByTestId('dashboard-edit-correct-save')).toBeDisabled()
    fireEvent.click(screen.getByTestId('dashboard-edit-correct-reconfirm'))
    await act(async () => { fireEvent.click(screen.getByTestId('dashboard-edit-correct-save')) })
    expect(correct).toHaveBeenCalledWith('atlas', 'Atlas', 'verdict', 'green', '', { page: 'package:report', version: 1 })
  })

  it('asks to confirm an open correction against the newer page before it saves', async () => {
    const correct = vi.spyOn(api, 'memberDashboardCorrect').mockResolvedValue({ ok: true } as never)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    const { rerender } = renderWithProviders(<Host onSend={vi.fn()} version={14} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    fireEvent.change(screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')[0], { target: { value: 'green' } })
    // Version 15 is shown now: the draft stays, but Save waits for a confirm.
    rerender(<Host onSend={vi.fn()} version={15} />)
    expect(screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')[0]).toHaveValue('green')
    expect(screen.getByTestId('dashboard-edit-correct-save')).toBeDisabled()
    fireEvent.click(screen.getByTestId('dashboard-edit-correct-reconfirm'))
    const save = screen.getByTestId('dashboard-edit-correct-save')
    expect(save).toBeEnabled()
    await act(async () => { fireEvent.click(save) })
    // Sent against the page the person confirmed, so the gateway takes it.
    expect(correct).toHaveBeenCalledWith('atlas', 'Atlas', 'verdict', 'green', '', { page: 'template:project-report', version: 15 })
  })

  it('adopts the newer page\'s field type when the person confirms a changed page', async () => {
    const correct = vi.spyOn(api, 'memberDashboardCorrect').mockResolvedValue({ ok: true } as never)
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    const { rerender, queryClient } = renderWithProviders(<Host onSend={vi.fn()} version={14} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    fireEvent.change(screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')[0], { target: { value: '42' } })
    // Version 15 makes the same field a number; the open lineage re-reads it.
    lineage.mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'number',
      hops: [], edit: { kind: 'correct', type: 'number', scalar: true },
    })
    rerender(<Host onSend={vi.fn()} version={15} />)
    await act(async () => { await queryClient.refetchQueries({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas', 'Atlas', 'lineage', 'verdict'] }) })
    fireEvent.click(await screen.findByTestId('dashboard-edit-correct-reconfirm'))
    await act(async () => { fireEvent.click(screen.getByTestId('dashboard-edit-correct-save')) })
    // Parsed as the field is now, against the page confirmed, with the draft kept.
    expect(correct).toHaveBeenCalledWith('atlas', 'Atlas', 'verdict', 42, '', { page: 'template:project-report', version: 15 })
  })

  it('keeps a correction draft but turns Save off while paused for a newer page', async () => {
    const correct = vi.spyOn(api, 'memberDashboardCorrect').mockResolvedValue({ ok: true } as never)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    const { rerender } = renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    fireEvent.change(screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')[0], { target: { value: 'green' } })
    rerender(<Host onSend={vi.fn()} paused />)
    expect(screen.getByTestId('dashboard-edit-correct-save')).toBeDisabled()
    expect(screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')[0]).toHaveValue('green')
    expect(screen.getByTestId('dashboard-edit-correct-hint')).toHaveTextContent(/newer version/i)
    fireEvent.click(screen.getByTestId('dashboard-edit-correct-save'))
    expect(correct).not.toHaveBeenCalled()
  })

  it('keeps a newer Ask draft when an earlier correction completes after its box closed', async () => {
    let finish: (v: unknown) => void = () => {}
    vi.spyOn(api, 'memberDashboardCorrect').mockImplementation(() => new Promise(resolve => { finish = resolve }) as never)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    fireEvent.change(screen.getByLabelText(/value/i), { target: { value: 'green' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-correct-save'))
    // Esc closes the correction box; then Ask is opened and a draft typed.
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'keep me' } })
    await act(async () => { finish({ ok: true }); await Promise.resolve() })
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('keep me')
  })

  it('keeps an open draft and picks nothing new while paused for a newer page', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    const { rerender } = renderWithProviders(<Host onSend={vi.fn()} />)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'keep me' } })
    rerender(<Host onSend={vi.fn()} paused />)
    // A new component tap opens nothing over the older page; the draft is kept.
    tap({ path: 'verdict', block: 'status', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['verdict'] })
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('keep me')
    expect(lineage).not.toHaveBeenCalledWith('atlas', 'Atlas', 'verdict')
  })

  it('traces nothing new while paused: no hover lineage and no tap card', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host onSend={vi.fn()} paused />)
    vi.useFakeTimers()
    try {
      pick({ path: 'accepted' })
      // Past the hover dwell: an unpaused layer would open the lineage by now.
      act(() => { vi.advanceTimersByTime(HOVER_DWELL_MS + 1) })
      tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
      expect(lineage).not.toHaveBeenCalled()
      expect(screen.queryByTestId('lineage-popover')).toBeNull()
      expect(screen.queryByTestId('dashboard-edit-flip')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the card open on "Sent" until the person closes it', async () => {
    const onSend = vi.fn().mockResolvedValue('sent')
    renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    expect(screen.getByTestId('dashboard-edit-flip-cancel')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    expect(await screen.findByText('Sent to Atlas.')).toBeInTheDocument()
    // Sent cannot be taken back, so no "Cancel" suggests it can: Done is the only exit.
    expect(screen.queryByTestId('dashboard-edit-flip-cancel')).toBeNull()
    // Still there well after the send: no timer closes it.
    vi.useFakeTimers()
    try {
      act(() => { vi.advanceTimersByTime(5000) })
      expect(screen.getByText('Sent to Atlas.')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
    fireEvent.click(screen.getByTestId('dashboard-edit-sent-done'))
    await waitFor(() => expect(screen.queryByTestId('dashboard-edit-comment')).toBeNull())
  })

  it('locks the request text from Send until the card closes', async () => {
    const onSend = vi.fn().mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    const box = await screen.findByTestId('dashboard-edit-comment')
    fireEvent.change(box, { target: { value: 'first' } })
    expect(box).not.toHaveAttribute('readonly')
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    await waitFor(() => expect(screen.getByTestId('dashboard-edit-comment')).toHaveAttribute('readonly'))
  })

  it('opens Edit when the frame forwards E from a focused value', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-edit-open')
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: EDIT_KEY_MESSAGE_TYPE }, source: frameWindow() }))
    })
    expect(await screen.findByTestId('lineage-edit-menu')).toBeInTheDocument()
  })

  it.each([
    ['fold', { kind: 'fold' }, ['Ask Atlas in chat to change how it\'s shown']],
    ['script', { kind: 'source', writer: 'cron:p99', job: 'p99' }, ['Ask Atlas in chat to change this', 'Correct the value now']],
    ['Bot-written', { kind: 'correct', type: 'string', scalar: true }, ['Ask Atlas in chat to change this', 'Correct the value now']],
  ])('names one action on a %s value, and each way to change it inside', async (_label, edit, rows) => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({ ...LINEAGE, edit } as DashboardLineage)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    // One label for what the button opens, never two outcomes joined by "or".
    const open = await screen.findByTestId('lineage-edit-open')
    expect(open).toHaveTextContent('Change this value…')
    expect(open.textContent).not.toMatch(/\bor\b/)
    fireEvent.click(open)
    const menu = screen.getByTestId('lineage-edit-menu')
    for (const row of rows) expect(menu).toHaveTextContent(row)
  })

  it('keeps an open draft when a late tap answer arrives', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-edit-open')
    // A tap whose answer the frame has not sent yet.
    const posted: { type?: string; seq?: number }[] = []
    const spy = vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m as { seq?: number }) })
    fireEvent.click(screen.getByTestId('dashboard-edit-capture'), { clientX: 20, clientY: 20 })
    spy.mockRestore()
    const seq = posted.find(m => m.type === 'kirocrew-dashboard:point')?.seq
    expect(seq).toBeGreaterThan(0)
    // Meanwhile the person opens Ask from the keyboard and types.
    fireEvent.keyDown(window, { key: 'e' })
    fireEvent.click(await screen.findByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'keep me' } })
    // The late answer names a component: it must not open that card over the draft.
    pick({ seq, path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    expect(screen.queryByTestId('dashboard-edit-flip')).toBeNull()
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('keep me')
  })

  it('ignores a frame Esc or click that would discard an open draft', async () => {
    renderWithProviders(<Host onSend={vi.fn()} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'unsent' } })
    // What a page script can post: the same messages the bridge sends.
    for (const type of ['kirocrew-dashboard:escape', ACTIVATE_MESSAGE_TYPE, EDIT_KEY_MESSAGE_TYPE]) {
      act(() => { window.dispatchEvent(new MessageEvent('message', { data: { type }, source: frameWindow() })) })
    }
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('unsent')
    // The person's own Esc in the host still closes it.
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('dashboard-edit-comment')).toBeNull())
  })

  it('ignores a frame Esc while Edit is open in the popover', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'unsent' } })
    act(() => { window.dispatchEvent(new MessageEvent('message', { data: { type: 'kirocrew-dashboard:escape' }, source: frameWindow() })) })
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('unsent')
  })

  it('does not offer a resend when the send was not confirmed', async () => {
    const onSend = vi.fn().mockResolvedValue('unconfirmed')
    renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    expect(await screen.findByTestId('dashboard-edit-send-unconfirmed')).toBeInTheDocument()
    expect(screen.getByTestId('dashboard-edit-send')).toBeDisabled()
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('paints a field picked from Sources over the Sources list that stays open beside it', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    // jsdom lays nothing out, so the panel's positioned ancestor is supplied.
    vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockImplementation(function (this: HTMLElement) { return this.parentElement })
    renderWithProviders(<Host />)
    fireEvent.click(screen.getByTestId('dashboard-edit-sources-button'))
    fireEvent.click(screen.getByTestId('dashboard-edit-source-accepted'))
    const popover = await screen.findByTestId('lineage-popover')
    const sources = screen.getByTestId('dashboard-edit-sources')
    const z = (el: HTMLElement) => Number(getComputedStyle(el).zIndex)
    expect(z(popover)).toBeGreaterThan(z(sources))
    expect(z(screen.getByTestId('lineage-connector') as unknown as HTMLElement)).toBeGreaterThan(z(sources))
  })

  it('says where a value came from in one sentence and keeps the steps behind Details', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    expect(await screen.findByTestId('lineage-summary')).toHaveTextContent("Counted from Atlas's activity.")
    const details = screen.getByTestId('lineage-details')
    expect(details).not.toHaveAttribute('open')
    expect(details).toContainElement(screen.getByTestId('lineage-hop-crew_log'))
  })

  it("names a script record's writer by the job's name, not its id", async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [{ kind: 'record', writer: 'cron:dd910859', at: '', found: true }],
      edit: { kind: 'source', writer: 'cron:dd910859', job: 'dd910859' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [{ id: 'dd910859', name: 'Nightly digest' }] } as never)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await waitFor(() => expect(screen.getByTestId('lineage-summary')).toHaveTextContent('Written by Nightly digest'))
    expect(screen.getByTestId('lineage-summary')).not.toHaveTextContent('dd910859')
  })

  it('marks a value past its stale limit as out of date', async () => {
    const past = '2020-01-01T00:00:00.000Z' // long past, so the check needs no clock
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [
        { kind: 'record', writer: 'cron:p99', at: '', found: true },
        { kind: 'freshness', stale_after: past, max_age_s: 7200, by: 'script' },
      ],
      edit: { kind: 'source', writer: 'cron:p99', job: 'p99' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [] })
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    // Said in the one-line summary, so a stale number never reads as current.
    expect(await screen.findByTestId('lineage-out-of-date')).toHaveTextContent(/Out of date: went stale .*ago/)
    // The raw timestamp is not what the person reads.
    expect(screen.getByTestId('lineage-popover')).not.toHaveTextContent(past)
  })

  it("says so when the schedule read behind a writer's name fails", async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [{ kind: 'record', writer: 'cron:dd910859', at: '', found: true }],
      edit: { kind: 'source', writer: 'cron:dd910859', job: 'dd910859' },
    })
    vi.spyOn(api, 'crons').mockRejectedValue(new Error('500'))
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    // The id alone would read as "no such job"; the failed read is said beside it.
    expect(await screen.findByTestId('lineage-summary-crons-failed')).toBeInTheDocument()
  })

  it('keeps measuring the frame after a new key replaces it', async () => {
    const observed: Element[] = []
    vi.stubGlobal('ResizeObserver', class { observe(el: Element) { observed.push(el) } disconnect() {} unobserve() {} })
    try {
      const { rerender } = renderWithProviders(<LayerFirstHost frameKey="a" />)
      const first = screen.getByTestId('frame')
      await waitFor(() => expect(observed).toContain(first))
      rerender(<LayerFirstHost frameKey="b" />)
      const second = screen.getByTestId('frame')
      expect(second).not.toBe(first)
      // The replacement frame is measured too, so the capture layer follows its size.
      await waitFor(() => expect(observed).toContain(second))
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it("shows a job's script by a name that does not reveal the host's folders", async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [{ kind: 'script', job: 'p99', script: '/home/someone/work/digest.py:run', run_at: '' }],
      edit: { kind: 'source', writer: 'cron:p99', job: 'p99' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [{ id: 'p99', name: 'p99', script: '/srv/jobs/digest.py:run' }] } as never)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    const popover = await screen.findByTestId('lineage-popover')
    await waitFor(() => expect(popover).toHaveTextContent('digest.py:run'))
    expect(popover).not.toHaveTextContent('/home/someone')
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-source'))
    const box = screen.getByTestId('lineage-source-box')
    await waitFor(() => expect(box).toHaveTextContent('digest.py:run'))
    expect(box).not.toHaveTextContent('/home/someone')
    expect(box).not.toHaveTextContent('/srv/jobs')
  })

  it('opens the job itself on the Schedule page', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [],
      edit: { kind: 'source', writer: 'cron:p99', job: 'p99' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [] })
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-source'))
    expect(screen.getByTestId('lineage-open-schedule')).toHaveAttribute('href', '/schedule?job=p99')
  })

  it('offers one control to change a value, named for what it does', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    expect(await screen.findByTestId('lineage-edit-open')).toHaveTextContent('Change this value…')
    expect(screen.queryByTestId('lineage-pencil')).toBeNull()
  })

  it('names every step in words a reader knows, open Details included', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    const popover = await screen.findByTestId('lineage-popover')
    await screen.findByTestId('lineage-summary')
    expect(popover.textContent).not.toMatch(/\bfold\b|gateway|needs:|cannot write/i)
    expect(screen.getByTestId('lineage-hop-crew_log')).toHaveTextContent('Not tracked yet')
  })

  it('keeps the pinned popover when a hover was still dwelling as Edit opened', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'accepted' })
    const open = await screen.findByTestId('lineage-edit-open')
    vi.useFakeTimers()
    try {
      pick({ path: 'verdict' })
      fireEvent.click(open)
      act(() => { vi.advanceTimersByTime(1000) })
    } finally {
      vi.useRealTimers()
    }
    expect(screen.getByTestId('lineage-ask')).toBeInTheDocument()
    expect(lineage).toHaveBeenCalledTimes(1)
    expect(lineage).toHaveBeenCalledWith('atlas', 'Atlas', 'accepted')
  })

  it('keeps an open draft when focus moves to another Sources field', async () => {
    vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockImplementation(function (this: HTMLElement) { return this.parentElement })
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host onSend={vi.fn()} />)
    fireEvent.click(screen.getByTestId('dashboard-edit-sources-button'))
    fireEvent.click(screen.getByTestId('dashboard-edit-source-accepted'))
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'keep me' } })
    fireEvent.focus(screen.getByTestId('dashboard-edit-source-verdict'))
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('keep me')
    expect(lineage).toHaveBeenCalledTimes(1)
  })

  it('drops an unpinned lineage on a wheel and asks the frame again after it scrolls', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-popover')
    const posted: { type?: string }[] = []
    vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m as { type?: string }) })
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { cb(0); return 0 })
    fireEvent.wheel(screen.getByTestId('dashboard-edit-capture'), { deltaY: 120, clientX: 20, clientY: 20 })
    expect(screen.queryByTestId('lineage-popover')).toBeNull()
    expect(posted.map(m => m.type)).toEqual(['kirocrew-dashboard:wheel', 'kirocrew-dashboard:point'])
  })

  it('scrolls the page on a touch drag and does not read the drag as a tap', () => {
    renderWithProviders(<Host />)
    const posted: { type?: string; dy?: number }[] = []
    vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m as { type?: string; dy?: number }) })
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { cb(0); return 0 })
    const capture = screen.getByTestId('dashboard-edit-capture')
    fireEvent.pointerDown(capture, { pointerType: 'touch', pointerId: 7, clientX: 20, clientY: 200 })
    fireEvent.pointerMove(capture, { pointerType: 'touch', pointerId: 7, clientX: 20, clientY: 150 })
    fireEvent.pointerUp(capture, { pointerType: 'touch', pointerId: 7, clientX: 20, clientY: 150 })
    fireEvent.click(capture, { clientX: 20, clientY: 150 })
    const wheels = posted.filter(m => m.type === 'kirocrew-dashboard:wheel')
    expect(wheels).toHaveLength(1)
    expect(wheels[0].dy).toBe(50)
    // The only point asked is the fresh pick after the scroll, not a tap's.
    expect(posted.filter(m => m.type === 'kirocrew-dashboard:point')).toHaveLength(1)
  })

  it('says a failed lineage read through the shared error notice, without a hand-off', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockRejectedValue(new Error('boom'))
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    const notice = await screen.findByTestId('lineage-trace-failed')
    // The layer sits on the Members page beside the DM composer, whose unsent draft
    // the hand-off's navigation to /chat would drop, so no notice offers it.
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('keeps a draft when Escape is pressed while the Dashboard is kept hidden', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    const { rerender } = renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    // The side panel keeps the visited Dashboard mounted but hidden behind Workspace.
    rerender(<Host hidden />)
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.keyDown(window, { key: 'Escape' })
    rerender(<Host />)
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('show CI state')
  })

  it('keeps a draft when Escape is pressed in a text box outside the layer', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<><textarea aria-label="Message Atlas" data-testid="composer" /><Host onSend={vi.fn()} /></>)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'keep me' } })
    // Escape in the chat composer beside the Dashboard belongs to the composer.
    fireEvent.keyDown(screen.getByTestId('composer'), { key: 'Escape' })
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('keep me')
  })

  it('keeps a draft when Escape closes another dialog opened over it', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<><div role="dialog" aria-modal="true"><button type="button" data-testid="modal-close">Close</button></div><Host onSend={vi.fn()} /></>)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'keep me' } })
    // Modal lets Escape travel on to window; on a dialog's button it is that dialog's.
    fireEvent.keyDown(screen.getByTestId('modal-close'), { key: 'Escape' })
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('keep me')
  })

  it('keeps a draft when Escape is pressed while the side panel is collapsed', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    const { rerender } = renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    // A live app tab keeps the collapsed panel mounted, hidden by display:none.
    rerender(<Host collapsed />)
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.keyDown(window, { key: 'Escape' })
    rerender(<Host />)
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('show CI state')
  })

  it('opens no edit menu from E while the side panel is collapsed', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    const { rerender } = renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-value')
    rerender(<Host collapsed />)
    fireEvent.keyDown(window, { key: 'e' })
    rerender(<Host />)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(screen.queryByTestId('lineage-edit-menu')).toBeNull()
  })

  it('offers no hand-off from a failed refresh while a request is being typed', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    const { queryClient } = renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    // A field push refetches the lineage and the refetch fails; the last lineage and
    // the draft stay on screen beside the notice.
    lineage.mockRejectedValue(new Error('boom'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas', 'Atlas', 'lineage', 'accepted'] }) })
    const notice = await screen.findByTestId('lineage-trace-failed')
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('show CI state')
  })
})
