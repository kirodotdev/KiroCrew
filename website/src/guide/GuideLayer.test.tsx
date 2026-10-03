/**
 * Registered-action guide: consent before anything moves, the arrow's target
 * and pointer contract, owner-tab / revision handling, which slot "is being
 * viewed", per-request guide headers on the ONE save a committed step names,
 * that the MCP guide only hands off to the native add form, and that the
 * browser never claims a save succeeded.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { useEffect, useState, type ReactNode } from 'react'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { store } from '../store'
import { setActiveSlot } from '../store/chatSlice'
import { TAB_ID } from '../api/tabId'
import { applyGuideUpdate, GUIDE_PENDING_QUERY_KEY, type Guide } from '../api/guide'
import { _resetViewedThreadForTests, setViewedThreadSlot } from '../lib/viewedThread'
import { i18nT } from '../i18n/t'
import { GuideProvider, useGuideRequestHeaders } from './GuideContext'
import GuideLayer, { arrowPlacement } from './GuideLayer'
import { GUIDE_ANCHORS, isSensitiveSetting, resolveGuideAction } from './guideActions'
import { GUIDE_TARGET_WAIT_MS } from './useGuideStepTracker'
import McpCustomServerModal from '../components/McpCustomServerModal'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { resolveSettingElementStrict } from '../hooks/useSettingHighlight'

const L = (k: string, v?: Record<string, unknown>) => i18nT(`components.guideLayer.${k}`, v)

type Call = { path: string; body: Record<string, unknown>; headers: Headers }
let calls: Call[]
let pending: Guide[]
/** What each write returns; defaults to echoing the guide the test set. */
let onWrite: (path: string, body: Record<string, unknown>) => Guide | null

const guide = (over: Partial<Guide> = {}): Guide => ({
  guide_id: 'g1',
  slot_key: 'slot-A',
  status: 'offered',
  revision: 1,
  owner_tab: null,
  action_index: 0,
  step_index: 0,
  actions: [{ id: 'crewmate.create', params: { name: 'radar', goal: 'watch the build' } }],
  reason: 'You asked for a crewmate.',
  expires_at: null,
  lease_expires_at: null,
  ...over,
})

const claimed = (g: Guide): Guide => ({ ...g, status: 'active', owner_tab: TAB_ID, revision: g.revision + 1 })

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

/**
 * The native MCP surfaces, reduced to what the guide touches: the Connections
 * tab (starting on Services), McpTab's Add Custom button, and the REAL add
 * dialog. The anchors are the same literals the pages carry (pinned below).
 */
function NativeMcpHost({ startOn = 'services' }: { startOn?: 'services' | 'mcp-servers' }) {
  const [tab, setTab] = useState(startOn)
  const [open, setOpen] = useState(false)
  return (
    <>
      <Target anchor="mcp.servers-tab" testId="native-mcp-tab" onClick={() => setTab('mcp-servers')} />
      {tab === 'mcp-servers' && <Target anchor="mcp.add-custom" testId="native-add-custom" onClick={() => setOpen(true)} />}
      <McpCustomServerModal open={open} onClose={() => setOpen(false)} />
    </>
  )
}

let qc: QueryClient
function renderGuide(path: string, extra?: ReactNode) {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <GuideProvider>
            <GuideLayer />
            <LocationProbe />
            {extra}
          </GuideProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

/** A registered control with a real-looking box (happy-dom lays nothing out). */
function Target({ anchor, rect = { top: 300, left: 100, width: 80, height: 30 }, testId, onClick }: {
  anchor?: string
  rect?: { top: number; left: number; width: number; height: number }
  testId?: string
  onClick?: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-guide-anchor={anchor}
      data-testid={testId ?? `target-${anchor ?? 'none'}`}
      ref={(el) => {
        if (!el) return
        el.getBoundingClientRect = () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height, x: rect.left, y: rect.top, toJSON: () => ({}) }) as DOMRect
      }}
    >
      target
    </button>
  )
}

beforeEach(() => {
  calls = []
  pending = []
  onWrite = () => null
  _resetViewedThreadForTests()
  store.dispatch(setActiveSlot('slot-A'))
  server.use(
    http.get('/api/guide/pending', () => HttpResponse.json({ guides: pending })),
    ...['claim', 'progress', 'heartbeat', 'cancel'].map(name =>
      http.post(`/api/guide/${name}`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: `/api/guide/${name}`, body, headers: request.headers })
        return HttpResponse.json({ guide: onWrite(name, body) })
      }),
    ),
    http.post('/api/mcp/custom', async ({ request }) => {
      calls.push({ path: '/api/mcp/custom', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      return HttpResponse.json({ ok: true, added: ['files'], enabled: false })
    }),
  )
})

afterEach(() => {
  vi.restoreAllMocks()
})

const writes = (path: string) => calls.filter(c => c.path === path)

describe('offer and consent', () => {
  it('offers the guide only in the chat it came from, and nothing moves before Start', async () => {
    pending = [guide()]
    renderGuide('/chat/slot-A')
    expect(await screen.findByTestId('guide-start')).toBeTruthy()
    // The offer says who proposed it: the pill floats over any page.
    expect(screen.getByTestId('guide-source')).toHaveTextContent('Guide from your assistant')
    // The gateway's internal reason token is never painted as copy.
    expect(screen.queryByText('You asked for a crewmate.')).toBeNull()
    // Offered is not accepted: no claim, no navigation, no pre-fill.
    await new Promise(r => setTimeout(r, 50))
    expect(calls).toHaveLength(0)
    expect(screen.getByTestId('loc').textContent).toBe('/chat/slot-A')
  })

  it('says so when the pending-guides read fails, and can be closed', async () => {
    server.use(http.get('/api/guide/pending', () => HttpResponse.json({ error: 'gateway unavailable' }, { status: 500 })))
    renderGuide('/chat/slot-A')
    expect(await screen.findByTestId('guide-pending-error')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: L('close') }))
    await waitFor(() => expect(screen.queryByTestId('guide-pending-error')).toBeNull())
  })

  it('stays silent when the owner-only pending route refuses this session', async () => {
    server.use(http.get('/api/guide/pending', () => HttpResponse.json({ error: 'owner only' }, { status: 403 })))
    renderGuide('/chat/slot-A')
    await new Promise(r => setTimeout(r, 100))
    expect(screen.queryByTestId('guide-pending-error')).toBeNull()
  })

  it('Start claims first, then navigates to the Crewmates hand-off with the draft', async () => {
    pending = [guide()]
    onWrite = (name, b) => (name === 'claim' ? claimed({ ...guide(), revision: b.revision as number }) : null)
    renderGuide('/chat/slot-A')
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/members?create=1&name=radar&goal=watch+the+build'))
    const [claim] = writes('/api/guide/claim')
    expect(claim.body).toEqual({ guide_id: 'g1', tab_id: TAB_ID, revision: 1 })
    expect(claim.body).not.toHaveProperty('take_over')
  })

  it('reads the Crewmates page thread, not a stale chat activeSlot', async () => {
    pending = [guide()]
    // activeSlot is still slot-A from an earlier chat visit, but on /members
    // the thread on screen is whatever the page registered.
    renderGuide('/members?member=radar')
    await act(async () => { await qc.refetchQueries({ queryKey: GUIDE_PENDING_QUERY_KEY }) })
    expect(screen.queryByTestId('guide-pill')).toBeNull()
    act(() => setViewedThreadSlot('slot-B'))
    expect(screen.queryByTestId('guide-pill')).toBeNull()
    act(() => setViewedThreadSlot('slot-A'))
    expect(await screen.findByTestId('guide-start')).toBeTruthy()
  })

  it('keeps the members address when entering from the Crewmates page', () => {
    const r = resolveGuideAction({ id: 'crewmate.create', params: { goal: 'g' } })
    if (!r.ok || r.action.enter.kind !== 'navigate') throw new Error('expected navigate')
    expect(r.action.enter.to({ pathname: '/members', search: '?member=default' })).toBe('/members?member=default&create=1&goal=g')
  })
})

async function startCrewmate(extra?: ReactNode, over: Partial<Guide> = {}) {
  pending = [guide(over)]
  let current = claimed(guide(over))
  onWrite = (name, b) => {
    if (name === 'claim') return current
    if (name === 'progress') {
      current = { ...current, revision: current.revision + 1, step_index: (b.step_index as number) + (b.outcome === 'observed' ? 1 : 0), status: b.outcome === 'observed' ? 'active' : 'target_missing' }
      return current
    }
    return current
  }
  renderGuide('/chat/slot-A', extra)
  fireEvent.click(await screen.findByTestId('guide-start'))
  await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('/members'))
  return () => current
}

describe('arrow and targets', () => {
  it('points at the registered control without a scrim or pointer capture, and follows it', async () => {
    const rect = { top: 300, left: 100, width: 80, height: 30 }
    await startCrewmate(<Target anchor={GUIDE_ANCHORS.crewmateGoalNext} rect={rect} />)
    const outline = await screen.findByTestId('guide-target-outline')
    const arrow = screen.getByTestId('guide-arrow')
    for (const el of [outline, arrow]) {
      expect(el.className).toContain('pointer-events-none')
      expect(el.getAttribute('aria-hidden')).toBe('true')
    }
    // No element of the layer covers the viewport.
    expect(document.body.querySelector('.inset-0')).toBeNull()
    expect(screen.getByTestId('guide-pill').className).not.toContain('fixed')
    expect(screen.getByTestId('guide-pill').parentElement).not.toBe(document.body)
    expect(outline.style.top).toBe('296px')
    // The page underneath still gets its click.
    const onClick = vi.fn()
    screen.getByTestId(`target-${GUIDE_ANCHORS.crewmateGoalNext}`).addEventListener('click', onClick)
    fireEvent.click(screen.getByTestId(`target-${GUIDE_ANCHORS.crewmateGoalNext}`))
    expect(onClick).toHaveBeenCalled()
    // Scrolling re-measures the same control.
    rect.top = 420
    act(() => { window.dispatchEvent(new Event('scroll')) })
    await waitFor(() => expect(screen.getByTestId('guide-target-outline').style.top).toBe('416px'))
  })

  it('reports observed once the form reaches a later registered control', async () => {
    await startCrewmate(<><Target anchor={GUIDE_ANCHORS.crewmateGoalNext} /><Target anchor={GUIDE_ANCHORS.crewmateNameNext} /></>)
    await waitFor(() => expect(writes('/api/guide/progress').length).toBeGreaterThan(0))
    const [first] = writes('/api/guide/progress')
    expect(first.body).toMatchObject({ guide_id: 'g1', tab_id: TAB_ID, action_index: 0, step_index: 0, outcome: 'observed' })
  })

  it('reports target_missing after the bounded wait and never points at a look-alike', async () => {
    // An unregistered button that looks like the step's Next is not a target.
    await startCrewmate(<Target testId="meet-crewmates-next" />)
    // Let the tracker start its wait, then move the clock past the bound.
    await new Promise(r => setTimeout(r, 300))
    expect(writes('/api/guide/progress')).toHaveLength(0)
    const t0 = Date.now()
    vi.spyOn(Date, 'now').mockImplementation(() => t0 + GUIDE_TARGET_WAIT_MS + 1000)
    await waitFor(() => expect(writes('/api/guide/progress').map(c => c.body.outcome)).toEqual(['target_missing']))
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    expect(await screen.findByText(L('target_missing'))).toBeTruthy()
  })

  it('resumes a target_missing guide once its control reappears, reporting observed only', async () => {
    function LateTarget() {
      const [shown, setShown] = useState(false)
      useEffect(() => {
        const on = () => setShown(true)
        window.addEventListener('test-show-target', on)
        return () => window.removeEventListener('test-show-target', on)
      }, [])
      return shown ? <Target anchor={GUIDE_ANCHORS.crewmateNameNext} /> : null
    }
    const now = await startCrewmate(<LateTarget />)
    await new Promise(r => setTimeout(r, 300))
    const t0 = Date.now()
    vi.spyOn(Date, 'now').mockImplementation(() => t0 + GUIDE_TARGET_WAIT_MS + 1000)
    await waitFor(() => expect(now().status).toBe('target_missing'))
    act(() => { window.dispatchEvent(new Event('test-show-target')) })
    await waitFor(() => expect(writes('/api/guide/progress').map(c => c.body.outcome)).toEqual(['target_missing', 'observed']))
    expect(now().status).toBe('active')
  })

  it('still attaches guide headers to the save once a committed step went target_missing', async () => {
    let read: (() => Record<string, string> | undefined) | null = null
    let readOther: (() => Record<string, string> | undefined) | null = null
    function HeaderProbe() {
      read = useGuideRequestHeaders('crewmate.create')
      readOther = useGuideRequestHeaders('mcp.open_add')
      return null
    }
    // Start at the commit step with its Create control absent, as after a reload.
    const now = await startCrewmate(<HeaderProbe />, { step_index: 2 })
    await new Promise(r => setTimeout(r, 300))
    const t0 = Date.now()
    vi.spyOn(Date, 'now').mockImplementation(() => t0 + GUIDE_TARGET_WAIT_MS + 1000)
    await waitFor(() => expect(now().status).toBe('target_missing'))
    await waitFor(() => expect(read?.()).toEqual({
      'X-Guide-Id': 'g1', 'X-Guide-Tab': TAB_ID, 'X-Guide-Revision': String(now().revision),
    }))
    // A different action's save never carries this guide's headers.
    expect(readOther?.()).toBeUndefined()
  })

  it('places the arrow below a control at the top edge and keeps it on screen', () => {
    expect(arrowPlacement({ top: 4, left: 0, width: 10, height: 10 }, { width: 320, height: 600 })).toMatchObject({ up: true, left: 4 })
    const p = arrowPlacement({ top: 300, left: 310, width: 20, height: 20 }, { width: 320, height: 600 })
    expect(p.up).toBe(false)
    expect(p.left).toBeLessThanOrEqual(320 - 24 - 4)
  })

  it('resolves a Settings row strictly: no first-match stand-in for a missing occurrence', () => {
    const entry = { ...SETTINGS_REGISTRY[0], settingId: undefined, configKey: undefined, labelKey: undefined, label: 'Dup', occurrence: 2 }
    const one = document.createElement('div')
    one.setAttribute('data-setting-label', 'Dup')
    document.body.appendChild(one)
    expect(resolveSettingElementStrict(entry)).toBeNull()
    const two = document.createElement('div')
    two.setAttribute('data-setting-label', 'Dup')
    document.body.appendChild(two)
    expect(resolveSettingElementStrict(entry)).toBe(two)
    one.remove(); two.remove()
  })

  it('never guides to credential or security-ceiling settings', () => {
    const sensitive = SETTINGS_REGISTRY.filter(isSensitiveSetting).map(e => e.id)
    for (const id of ['security.denied-commands', 'secrets.jira-api-token', 'browser.attach-token', 'channels.slack-bot-token-slack']) {
      expect(sensitive).toContain(id)
      expect(resolveGuideAction({ id: 'settings.show', params: { setting_id: id } })).toEqual({ ok: false, reason: 'sensitive_setting' })
    }
    expect(resolveGuideAction({ id: 'settings.show', params: { setting_id: 'chat.default-model' } }).ok).toBe(true)
    expect(resolveGuideAction({ id: 'run.js', params: {} })).toEqual({ ok: false, reason: 'unknown_action' })
  })
})

describe('owner tab and revision', () => {
  it('a second tab cannot advance; explicit takeover claims the current revision', async () => {
    pending = [guide({ status: 'active', owner_tab: 'other-tab', revision: 5 })]
    onWrite = () => claimed(guide({ revision: 5 }))
    renderGuide('/chat/slot-A', <><Target anchor={GUIDE_ANCHORS.crewmateGoalNext} /><Target anchor={GUIDE_ANCHORS.crewmateNameNext} /></>)
    expect(await screen.findByText(L('other_tab'))).toBeTruthy()
    await new Promise(r => setTimeout(r, 400))
    expect(writes('/api/guide/progress')).toHaveLength(0)
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    fireEvent.click(screen.getByTestId('guide-take-over'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    expect(writes('/api/guide/claim')[0].body).toEqual({ guide_id: 'g1', tab_id: TAB_ID, revision: 5, take_over: true })
  })

  it('the old tab stops once another tab takes the guide over', async () => {
    await startCrewmate(<Target anchor={GUIDE_ANCHORS.crewmateGoalNext} />)
    await screen.findByTestId('guide-arrow')
    act(() => applyGuideUpdate(qc, { ...claimed(guide()), owner_tab: 'other-tab', revision: 9 }))
    await waitFor(() => expect(screen.queryByTestId('guide-arrow')).toBeNull())
    const before = calls.length
    await new Promise(r => setTimeout(r, 400))
    expect(calls.slice(before).filter(c => c.path === '/api/guide/progress')).toHaveLength(0)
  })

  it('ignores an update older than the revision it holds', async () => {
    pending = [guide({ revision: 4 })]
    renderGuide('/chat/slot-A')
    await screen.findByTestId('guide-start')
    act(() => applyGuideUpdate(qc, guide({ revision: 3, status: 'cancelled' })))
    expect(screen.getByTestId('guide-start')).toBeTruthy()
  })
})

/** The native add form carries no box in happy-dom; give it one. */
function layOutCustomForm() {
  const form = document.querySelector<HTMLElement>('[data-guide-anchor="mcp.custom-form"]')
  if (!form) return null
  form.getBoundingClientRect = () => ({ top: 100, left: 100, width: 400, height: 300, right: 500, bottom: 400, x: 100, y: 100, toJSON: () => ({}) }) as DOMRect
  return form
}

describe('MCP: navigate to the native add form, never install', () => {
  const mcpGuide = (over: Partial<Guide> = {}) => guide({ actions: [{ id: 'mcp.open_add', params: {} }], ...over })

  it('resolves only with empty params and navigates to the existing MCP page', () => {
    const r = resolveGuideAction({ id: 'mcp.open_add', params: {} })
    if (!r.ok) throw new Error('expected ok')
    expect(r.action.enter.to({ pathname: '/chat/slot-A', search: '' })).toBe('/capabilities?tab=mcp')
    expect(r.action.steps.map(st => st.complete.kind)).toEqual(['reach', 'reach'])
    expect(r.action.steps.some(st => st.complete.kind === 'committed' || st.complete.kind === 'ack')).toBe(false)
    for (const params of [{ name: 'files' }, { spec: { command: 'x' } }, { name: 'files', spec: { command: 'x' } }]) {
      expect(resolveGuideAction({ id: 'mcp.open_add', params })).toEqual({ ok: false, reason: 'invalid_params' })
    }
    expect(resolveGuideAction({ id: 'mcp.add_review', params: { name: 'files', spec: { command: 'x' } } })).toEqual({ ok: false, reason: 'unknown_action' })
  })

  it('walks Services -> MCP Servers tab -> Add Custom -> empty native form, with zero MCP writes', async () => {
    pending = [mcpGuide()]
    let current = claimed(mcpGuide())
    onWrite = (name, b) => {
      if (name === 'progress') {
        const last = (b.step_index as number) >= 1
        current = { ...current, revision: current.revision + 1, step_index: last ? current.step_index : (b.step_index as number) + 1, status: last ? 'completed' : 'active' }
      }
      return current
    }
    renderGuide('/chat/slot-A', <NativeMcpHost />)
    await screen.findByTestId('guide-start')
    expect(document.querySelector('[data-guide-anchor="mcp.custom-form"]')).toBeNull()
    fireEvent.click(screen.getByTestId('guide-start'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/capabilities?tab=mcp'))

    // Step 1: the arrow is on the existing MCP Servers tab; nothing auto-opens.
    expect(await screen.findByText(L('step_mcp_open_tab'))).toBeTruthy()
    await new Promise(r => setTimeout(r, 400))
    expect(writes('/api/guide/progress')).toHaveLength(0)
    expect(document.querySelector('[data-guide-anchor="mcp.custom-form"]')).toBeNull()
    fireEvent.click(screen.getByTestId('native-mcp-tab'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed' })

    // Step 2: the arrow is on the existing Add Custom button; the human opens it.
    expect(await screen.findByText(L('step_mcp_add_custom'))).toBeTruthy()
    expect(document.querySelector('[data-guide-anchor="mcp.custom-form"]')).toBeNull()
    fireEvent.click(screen.getByTestId('native-add-custom'))
    await waitFor(() => expect(layOutCustomForm()).not.toBeNull())
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })

    // The native form is exactly as an unguided open leaves it: nothing pre-filled.
    const editor = screen.getByRole('textbox', { name: i18nT('components.mcpCustomServerModal.servers_json') }) as HTMLTextAreaElement
    expect(editor.value).toBe('')
    expect(screen.queryByLabelText(i18nT('components.mcpCustomServerModal.server_name_2'))).toBeNull()
    // Completion says the guide ended, never that a server was added.
    expect(await screen.findByText(L('finished_completed'))).toBeTruthy()
    expect(screen.queryByTestId('guide-saved-server')).toBeNull()
    expect(writes('/api/mcp/custom')).toHaveLength(0)
    for (const c of calls) expect(c.headers.get('X-Guide-Id')).toBeNull()

    // The pending list serves live guides only; a focus refetch must not take
    // the completion pill away while the user is reading it.
    pending = []
    await act(async () => { await qc.invalidateQueries({ queryKey: GUIDE_PENDING_QUERY_KEY }) })
    expect(qc.getQueryData(GUIDE_PENDING_QUERY_KEY)).toEqual([])
    // Past any exit animation: the pill must still be there, not fading out.
    await new Promise(r => setTimeout(r, 600))
    expect(screen.getByText(L('finished_completed'))).toBeTruthy()
  })

  it('skips the tab step when the MCP Servers view is already showing', async () => {
    pending = [mcpGuide()]
    onWrite = () => claimed(mcpGuide())
    renderGuide('/chat/slot-A', <NativeMcpHost startOn="mcp-servers" />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed' })
    expect(writes('/api/mcp/custom')).toHaveLength(0)
  })

  it('the native pages carry the registered anchors and no guide prefill', () => {
    const src = (rel: string) => readFileSync(path.join(__dirname, rel), 'utf-8')
    const connections = src('../pages/connections/ConnectionsPage.tsx')
    const mcpTab = src('../pages/overview/McpTab.tsx')
    const modal = src('../components/McpCustomServerModal.tsx')
    expect(connections).toContain(`data-guide-anchor="${GUIDE_ANCHORS.mcpServersTab}"`)
    expect(mcpTab).toContain(`data-guide-anchor="${GUIDE_ANCHORS.mcpAddCustom}"`)
    expect(modal).toContain(`'${GUIDE_ANCHORS.mcpCustomForm}'`)
    for (const text of [connections, mcpTab, modal]) {
      expect(text).not.toMatch(/guide\/GuideContext|useGuide|X-Guide|draft=/)
    }
  })
})
