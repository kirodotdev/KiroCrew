/**
 * Registered-action guide, the offer and the finish: the MCP guide only
 * hands off to the native add form, the in-chat offer card and its result
 * line, a surface the guide opened, the way back once a guide ends, and the
 * offering crewmate's own words in a guide.
 *
 * Shared fixtures: src/test/GuideLayerTestUtils.tsx. This file keeps its own
 * word-for-word copy of the guide state its tests reassign (pending,
 * onWrite, qc, calls) and of every helper that reads or assigns it
 * (renderGuide, writes, startCrewmate, the top-level hooks), plus L: an
 * import cannot be reassigned, src/i18n/dynamicKeys.test.ts allows L's
 * assembled key only in a .test. file.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, useNavigate } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { useState, type ReactNode } from 'react'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { store } from '../store'
import { setActiveSlot } from '../store/chatSlice'
import { TAB_ID } from '../api/tabId'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { applyGuideUpdate, GUIDE_PENDING_QUERY_KEY, type Guide } from '../api/guide'
import { _resetViewedThreadForTests, clearViewedThreadSlot, setViewedThreadSlot } from '../lib/viewedThread'
import { i18nT } from '../i18n/t'
import { endedBySlot, GuideProvider, reconcilePending } from './GuideContext'
import { useGuideOpenedSurface } from './useGuideOpenedSurface'
import GuideLayer, { GUIDE_FINISHED_DISMISS_MS, guideReturnFor, memberSlugOfSlot } from './GuideLayer'
import GuideOfferCard from './GuideOfferCard'
import { GUIDE_ANCHORS, resolveGuideAction } from './guideActions'
import McpCustomServerModal from '../components/McpCustomServerModal'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import {
  tickTracker,
  waitFor,
  guide,
  claimed,
  MCP_OPEN,
  LocationProbe,
  Target,
  T0,
} from '../test/GuideLayerTestUtils'
import type { Call } from '../test/GuideLayerTestUtils'
import { readPageFamily } from '../test/pageFamily'

const L = (k: string, v?: Record<string, unknown>) => i18nT(`components.guideLayer.${k}`, v)
let calls: Call[]
let pending: Guide[]
/** What each write returns; defaults to echoing the guide the test set. */
let onWrite: (path: string, body: Record<string, unknown>) => Guide | null

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
/** *chatSlot* stands in for the open chat pane, whose conversation holds the
 *  offer's row (guide `g1`, the id every fixture here uses). */
function renderGuide(path: string, extra?: ReactNode, chatSlot: string | null = 'slot-A') {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <NavigationLeaveGuardProvider>
            <GuideProvider>
              <GuideLayer />
              {chatSlot && <GuideOfferCard guideId="g1" slotKey={chatSlot} />}
              <LocationProbe />
              {extra}
            </GuideProvider>
          </NavigationLeaveGuardProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(T0)
  calls = []
  pending = []
  onWrite = () => null
  _resetViewedThreadForTests()
  store.dispatch(setActiveSlot('slot-A'))
  server.use(
    http.get('/api/guide/pending', () => HttpResponse.json({ guides: pending })),
    ...['claim', 'progress', 'heartbeat', 'cancel', 'dismiss', 'replan'].map(name =>
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
  vi.useRealTimers()
})

const writes = (path: string) => calls.filter(c => c.path === path)

async function startCrewmate(extra?: ReactNode, over: Partial<Guide> = {}, at = '/members') {
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
  await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain(at))
  return () => current
}

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
    // Into the form the button opens: the guide ends on its JSON box (where
    // the env variables go), and only the person's Done ends it there.
    expect(r.action.steps.map(st => st.complete.kind)).toEqual(['reach', 'reach', 'ack'])
    expect(r.action.steps[2].target).toEqual({ kind: 'anchor', anchor: 'mcp.custom-json' })
    expect(r.action.steps.some(st => st.complete.kind === 'committed')).toBe(false)
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
        const last = (b.step_index as number) >= 2
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
    await tickTracker(2)
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
    // The native form is exactly as an unguided open leaves it: nothing pre-filled.
    const editor = screen.getByRole('textbox', { name: i18nT('components.mcpCustomServerModal.servers_json') }) as HTMLTextAreaElement
    expect(editor.value).toBe('')
    editor.getBoundingClientRect = () => ({ top: 160, left: 110, width: 380, height: 200, right: 490, bottom: 360, x: 110, y: 160, toJSON: () => ({}) }) as DOMRect
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })

    // Step 3: the arrow is on the JSON box inside the form; Done ends the guide.
    expect(await screen.findByText(L('step_mcp_custom_json'))).toBeTruthy()
    await tickTracker(2)
    expect(writes('/api/guide/progress')).toHaveLength(2)
    fireEvent.click(screen.getByTestId('guide-next'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(3))
    expect(writes('/api/guide/progress')[2].body).toMatchObject({ step_index: 2, outcome: 'observed' })
    expect(screen.queryByLabelText(i18nT('components.mcpCustomServerModal.server_name_2'))).toBeNull()
    // Completion says the guide ended, never that a server was added.
    expect((await screen.findByTestId('guide-result')).textContent).toContain(L('finished_completed'))
    expect(screen.queryByTestId('guide-saved-server')).toBeNull()
    expect(writes('/api/mcp/custom')).toHaveLength(0)
    for (const c of calls) expect(c.headers.get('X-Guide-Id')).toBeNull()

    // The gateway keeps serving the ended guide (its chat's result line), so a
    // focus refetch must not take the line away while the user is reading it.
    pending = (qc.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY) ?? []).filter(g => g.status === 'completed')
    expect(pending).toHaveLength(1)
    await act(async () => { await qc.invalidateQueries({ queryKey: GUIDE_PENDING_QUERY_KEY }) })
    // Past any exit animation: the line must still be there, not fading out.
    await tickTracker(3)
    expect(screen.getByTestId('guide-result').textContent).toContain(L('finished_completed'))
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
    const connections = readPageFamily('ConnectionsPage', path.join(__dirname, '../pages/connections'))
    const mcpTab = src('../pages/overview/McpTab.tsx')
    const modal = src('../components/McpCustomServerModal.tsx')
    expect(connections).toContain(`guideAnchor('${GUIDE_ANCHORS.mcpServersTab}')`)
    expect(mcpTab).toContain(`guideAnchor('${GUIDE_ANCHORS.mcpAddCustom}')`)
    expect(modal).toContain(`'${GUIDE_ANCHORS.mcpCustomForm}'`)
    for (const text of [connections, mcpTab, modal]) {
      expect(text).not.toMatch(/guide\/GuideContext|useGuide|X-Guide|draft=/)
    }
  })
})

describe('in-chat offer card', () => {
  it('shows no top banner and no card in another slot\'s chat', async () => {
    pending = [guide()]
    // Split view: slot-A's pane and slot-B's pane are both open, slot-A viewed.
    renderGuide('/chat/slot-A', <div data-testid="pane-A"><GuideOfferCard guideId="g1" slotKey="slot-A" /></div>, 'slot-B')
    const start = await screen.findByTestId('guide-start')
    expect(screen.getByTestId('pane-A').contains(start)).toBe(true)
    expect(screen.getAllByTestId('guide-offer-card')).toHaveLength(1)
    expect(screen.queryByTestId('guide-pill')).toBeNull()
  })

  it('waits in slot A\'s chat while the user views slot B', async () => {
    store.dispatch(setActiveSlot('slot-B'))
    pending = [guide()]
    renderGuide('/chat/slot-B')
    const card = await screen.findByTestId('guide-offer-card')
    expect(card.getAttribute('data-guide-status')).toBe('offered')
    expect(card.textContent).toContain(i18nT('components.guideLayer.title_crewmate_create'))
    expect(screen.queryByTestId('guide-offer-hint')).toBeNull()
    expect(screen.queryByTestId('guide-pill')).toBeNull()
  })

  it('Start claims this slot\'s guide even when another slot is viewed', async () => {
    store.dispatch(setActiveSlot('slot-B'))
    pending = [guide()]
    onWrite = (name) => (name === 'claim' ? claimed(guide()) : null)
    renderGuide('/chat/slot-B')
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    expect(writes('/api/guide/claim')[0].body).toEqual({ guide_id: 'g1', tab_id: TAB_ID, revision: 1 })
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('/members'))
  })

  it('Dismiss cancels and the card is REPLACED by one result line', async () => {
    pending = [guide()]
    onWrite = (name) => (name === 'cancel' ? guide({ status: 'cancelled', revision: 2, finished_at: 10 }) : null)
    renderGuide('/chat/slot-A')
    fireEvent.click(await screen.findByTestId('guide-dismiss'))
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(1))
    expect(writes('/api/guide/claim')).toHaveLength(0)
    const line = await screen.findByTestId('guide-result')
    expect(line.textContent).toContain(L('finished_cancelled'))
    await waitFor(() => expect(document.activeElement).toBe(line))
    // One element in the chat: the offer's shell is gone, not wrapping the line.
    expect(screen.queryByTestId('guide-offer-card')).toBeNull()
    expect(screen.queryByTestId('guide-start')).toBeNull()
    expect(screen.getByTestId('guide-offer-cards').children).toHaveLength(1)
    expect(screen.getByTestId('guide-result-row').getAttribute('data-guide-status')).toBe('cancelled')
  })

  it('a cancelled guide is one muted line, and one a newer offer replaced leaves nothing', () => {
    pending = []
    const done = (reason: string | null) => renderGuide('/chat/slot-A', <GuideOfferCard guideId="gx" slotKey="slot-A" recordedStatus="cancelled" recordedReason={reason} />, null)
    const { unmount } = done('cancelled_by_user')
    const row = screen.getByTestId('guide-result-row')
    expect(row.tagName).toBe('P')
    expect(row.className).not.toContain('border')
    unmount()
    done('superseded')
    expect(screen.queryByTestId('guide-result-row')).toBeNull()
  })

  it('a completed show-me guide offers Show again, which replays and walks it again', async () => {
    const show = [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }]
    pending = [guide({ actions: show, status: 'completed', revision: 4, finished_at: 10 })]
    server.use(http.post('/api/guide/replay', async ({ request }) => {
      calls.push({ path: '/api/guide/replay', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      return HttpResponse.json({ guide: guide({ actions: show, status: 'offered', revision: 5 }) })
    }))
    onWrite = (name) => (name === 'claim' ? guide({ actions: show, status: 'active', revision: 6, owner_tab: TAB_ID }) : null)
    renderGuide('/chat/slot-A')
    fireEvent.click(await screen.findByTestId('guide-replay'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    expect(writes('/api/guide/replay')[0].body).toEqual({ guide_id: 'g1', revision: 4 })
    expect(writes('/api/guide/claim')[0].body).toEqual({ guide_id: 'g1', tab_id: TAB_ID, revision: 5 })
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('/settings'))
  })

  it('a Show again that fails says so on the result line it was pressed on', async () => {
    const show = [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }]
    pending = [guide({ actions: show, status: 'completed', revision: 4, finished_at: 10 })]
    server.use(http.post('/api/guide/replay', () => HttpResponse.json({ error: 'gateway busy' }, { status: 503 })))
    renderGuide('/chat/slot-A')
    expect(screen.queryByTestId('guide-line-error')?.textContent ?? '').toBe('')
    fireEvent.click(await screen.findByTestId('guide-replay'))
    await waitFor(() => expect(screen.getByTestId('guide-line-error').textContent).not.toBe(''))
    expect(screen.getByTestId('guide-replay')).toBeTruthy()
    // No hand-off: navigating to a chat would discard the composer draft beside it.
    expect(within(screen.getByTestId('guide-line-error')).queryByRole('button')).toBeNull()
  })

  it('a guide that saved a change is never offered again', async () => {
    pending = [guide({ status: 'completed', revision: 4, finished_at: 10 })]
    renderGuide('/chat/slot-A')
    await screen.findByTestId('guide-result')
    expect(screen.queryByTestId('guide-replay')).toBeNull()
  })

  it('the result line survives a page reload, and has no close control', async () => {
    // What the gateway serves after the reload: the slot's newest ended guide.
    pending = [guide({ status: 'cancelled', revision: 2, finished_at: 10 })]
    renderGuide('/chat/slot-A')
    const line = await screen.findByTestId('guide-result')
    expect(line.textContent).toContain(L('finished_cancelled'))
    expect(screen.queryByTestId('guide-offer-card')).toBeNull()
    // It is the conversation's record of the offer, so nothing hides it.
    expect(screen.queryByRole('button', { name: L('close') })).toBeNull()
  })

  it('once the gateway forgets the guide, the row draws the status it recorded', async () => {
    pending = []
    render(
      <Provider store={store}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <MemoryRouter initialEntries={['/chat/slot-A']}>
            <GuideProvider>
              <div data-testid="done"><GuideOfferCard guideId="g1" slotKey="slot-A" recordedStatus="completed" /></div>
              <div data-testid="lapsed"><GuideOfferCard guideId="g2" slotKey="slot-A" recordedStatus="offered" /></div>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    const done = await screen.findByText(L('finished_completed'))
    expect(screen.getByTestId('done').contains(done)).toBe(true)
    // An offer the store forgot never finished: it reads as expired, with no Start.
    await waitFor(() => expect(screen.getByTestId('lapsed').textContent).toContain(L('finished_expired')))
    expect(screen.queryByTestId('guide-start')).toBeNull()
  })

  it('a row in another slot\'s chat never draws this slot\'s live offer', async () => {
    pending = [guide()]
    renderGuide('/chat/slot-A', <div data-testid="pane-B"><GuideOfferCard guideId="g1" slotKey="slot-B" recordedStatus="offered" /></div>)
    await screen.findByTestId('guide-start')
    expect(screen.getAllByTestId('guide-start')).toHaveLength(1)
    expect(screen.getByTestId('pane-B').querySelector('[data-testid="guide-start"]')).toBeNull()
  })

  it('a read that left before the cancel answered cannot bring the offer back', () => {
    const cancelled = guide({ status: 'cancelled', revision: 2 })
    expect(reconcilePending([cancelled], [guide()])).toEqual([cancelled])
    // A newer server row wins, and a row the server dropped is dropped.
    const newer = guide({ revision: 3 })
    expect(reconcilePending([cancelled], [newer])).toEqual([newer])
    expect(reconcilePending([cancelled], [])).toEqual([])
  })

  it('a slot shows only its newest ended guide, and none while a guide is live', () => {
    const old = guide({ guide_id: 'g0', status: 'cancelled', finished_at: 5 })
    const recent = guide({ guide_id: 'g1', status: 'expired', finished_at: 9 })
    expect(endedBySlot([recent, old], new Set())['slot-A'].guide_id).toBe('g1')
    expect(endedBySlot([recent, old], new Set(['g1']))['slot-A'].guide_id).toBe('g0')
    expect(endedBySlot([old, guide({ guide_id: 'g2' })], new Set())['slot-A']).toBeUndefined()
    expect(endedBySlot([{ ...recent, dismissed: true }], new Set())['slot-A']).toBeUndefined()
  })

  it('a completed mate guide leaves the created line in the chat', async () => {
    pending = [guide()]
    renderGuide('/chat/slot-A')
    await screen.findByTestId('guide-start')
    const done = guide({ status: 'completed', revision: 5, actions: [{ id: 'crewmate.create', params: {}, result: { name: 'radar' } }] })
    act(() => applyGuideUpdate(qc, done))
    expect(await screen.findByText(L('finished_completed'))).toBeTruthy()
    expect(screen.getByTestId('guide-result-crewmate').textContent).toBe(i18nT('components.meetCrewmatesFlow.step4_title', { name: 'radar' }))
    expect(screen.queryByTestId('guide-pill')).toBeNull()
  })
})

describe('a surface the guide opened', () => {
  /** Stands in for the Crewmates page's creation flow. */
  function Surface({ pristine, onClose }: { pristine: boolean; onClose: () => void }) {
    const [open, setOpen] = useState(false)
    const mark = useGuideOpenedSurface('crewmate.create', {
      open,
      isPristine: () => pristine,
      close: () => { setOpen(false); onClose() },
    })
    return (
      <>
        <button type="button" data-testid="open-surface" onClick={() => { mark(); setOpen(true) }}>open</button>
        {open && <div data-testid="surface" />}
      </>
    )
  }

  const owned = () => guide({ status: 'active', owner_tab: TAB_ID, revision: 2 })

  it.each([
    ['cancelled', true, true],
    ['expired', true, true],
    ['cancelled', false, false],
    ['completed', true, false],
    // Ended because its save went through: the ready step stays on screen.
    ['saved_without_guide', true, false],
  ] as const)('a %s guide, untouched=%s, closes it: %s', async (status, pristine, closes) => {
    pending = [owned()]
    const onClose = vi.fn()
    renderGuide('/members', <Surface pristine={pristine} onClose={onClose} />, null)
    await waitFor(() => expect(qc.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY)).toHaveLength(1))
    fireEvent.click(screen.getByTestId('open-surface'))
    expect(screen.getByTestId('surface')).toBeTruthy()
    act(() => applyGuideUpdate(qc, guide(status === 'saved_without_guide'
      ? { status: 'cancelled', reason: 'saved_without_guide', revision: 3 }
      : { status, revision: 3 })))
    if (closes) {
      await waitFor(() => expect(screen.queryByTestId('surface')).toBeNull())
      expect(onClose).toHaveBeenCalledTimes(1)
    } else {
      await tickTracker(1)
      expect(screen.getByTestId('surface')).toBeTruthy()
      expect(onClose).not.toHaveBeenCalled()
    }
  })

  it('never closes a surface the user opened themselves', async () => {
    pending = []
    const onClose = vi.fn()
    renderGuide('/members', <Surface pristine onClose={onClose} />, null)
    fireEvent.click(screen.getByTestId('open-surface'))
    act(() => applyGuideUpdate(qc, guide({ status: 'cancelled', revision: 3 })))
    await tickTracker(1)
    expect(screen.getByTestId('surface')).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('the Mates page wires it to the New mate card and its edited flag', () => {
    const src = readFileSync(path.join(__dirname, '../pages/members/MembersPage.tsx'), 'utf-8')
    expect(src).toMatch(/useGuideOpenedSurface\('crewmate\.create'/)
    expect(src).toMatch(/isPristine: \(\) => !createEditedRef\.current && !createBusyRef\.current/)
    expect(src).toMatch(/markGuideOpenedRef\.current\(\)\n\s+openCreate\(/)
  })
})

describe('the way back once a guide ends', () => {
  const pilotRow = { name: 'pilot', kiro_agent: 'kirocrew', slug: 'pilot', slot_key: 'slot-pilot', running: false }
  const radarRow = { name: 'radar', kiro_agent: 'kirocrew', slug: 'radar', slot_key: 'slot-radar', running: false }

  beforeEach(() => {
    server.use(http.get('/api/members', () => HttpResponse.json({ members: [pilotRow, radarRow] })))
  })

  /** Owns *slot*'s guide in this tab, away from every chat, and lets it end. */
  async function endOwnedGuide(slot: string, how: 'complete' | 'cancel', at = '/settings/display/theme', extra?: ReactNode) {
    const owned = claimed(guide({ slot_key: slot }))
    pending = [owned]
    onWrite = (name) => (name === 'cancel' ? { ...owned, status: 'cancelled', revision: owned.revision + 1, finished_at: 10 } : null)
    renderGuide(at, extra, null)
    await screen.findByTestId('guide-continue')
    if (how === 'cancel') {
      fireEvent.click(screen.getByRole('button', { name: L('cancel_guide') }))
      await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(1))
    } else {
      act(() => applyGuideUpdate(qc, { ...owned, status: 'completed', revision: owned.revision + 1, finished_at: 10 }))
    }
  }

  describe('the finish chip does not linger', () => {
    function TwoWays() {
      const navigate = useNavigate()
      return (
        <>
          <button type="button" data-testid="go-a" onClick={() => navigate('/schedule')}>a</button>
          <button type="button" data-testid="go-b" onClick={() => navigate('/artifacts')}>b</button>
        </>
      )
    }
    afterEach(() => vi.useRealTimers())

    it('leaves by itself after a while, without a dismissal write', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete')
      await screen.findByTestId('guide-back')
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS - 1_000) })
      expect(screen.queryByTestId('guide-back')).not.toBeNull()
      act(() => { vi.advanceTimersByTime(1_500) })
      expect(screen.queryByTestId('guide-pill')).toBeNull()
      expect(writes('/api/guide/dismiss')).toHaveLength(0)
    })

    it('stays while focus is in it, so it never vanishes from under the keyboard', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete')
      const back = await screen.findByTestId('guide-back')
      act(() => back.focus())
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS + 500) })
      expect(screen.queryByTestId('guide-back')).not.toBeNull()
    })

    it('a click that left focus in it does not hold it: Done, then focus on the way back', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete')
      const back = await screen.findByTestId('guide-back')
      fireEvent.pointerDown(back)
      act(() => back.focus())
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS + 500) })
      expect(screen.queryByTestId('guide-pill')).toBeNull()
      // Focus goes somewhere real, never the body.
      expect(document.activeElement).not.toBe(back)
    })

    it('counts from the end, not from the last time it was shown: hidden and shown again it still leaves on time', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete')
      await screen.findByTestId('guide-back')
      act(() => { vi.advanceTimersByTime(6_000) })
      // The chat it came from comes on screen and goes again (a route change
      // settling): the chip hides and returns.
      act(() => setViewedThreadSlot('slot-pilot'))
      expect(screen.queryByTestId('guide-back')).toBeNull()
      act(() => clearViewedThreadSlot('slot-pilot'))
      await screen.findByTestId('guide-back')
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS - 6_000 + 500) })
      expect(screen.queryByTestId('guide-pill')).toBeNull()
    })

    it('a route change right after the end does not restart the countdown', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete', '/settings/display/theme', <TwoWays />)
      await screen.findByTestId('guide-back')
      fireEvent.click(screen.getByTestId('go-a'))
      await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/schedule'))
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS + 500) })
      expect(screen.queryByTestId('guide-pill')).toBeNull()
    })

    it('survives the first move to another page and leaves on the second', async () => {
      await endOwnedGuide('slot-pilot', 'complete', '/settings/display/theme', <TwoWays />)
      await screen.findByTestId('guide-back')
      fireEvent.click(screen.getByTestId('go-a'))
      await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/schedule'))
      expect(screen.queryByTestId('guide-back')).not.toBeNull()
      fireEvent.click(screen.getByTestId('go-b'))
      await waitFor(() => expect(screen.queryByTestId('guide-pill')).toBeNull())
    })
  })

  it.each([['complete', 'finished_completed'], ['cancel', 'finished_cancelled']] as const)(
    '%s from a mate\'s chat: the way back lands on that mate\'s thread and keeps its result line',
    async (how, finishedKey) => {
      await endOwnedGuide('slot-radar', how)
      const back = await screen.findByTestId('guide-back')
      expect(screen.getByTestId('guide-pill').textContent).toContain(L(finishedKey))
      expect(back.getAttribute('data-guide-return')).toBe('chat')
      expect(back.textContent).toBe(i18nT('components.meetCrewmatesFlow.back_to_chat'))
      fireEvent.click(back)
      await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/members?member=radar'))
      await waitFor(() => expect(screen.queryByTestId('guide-pill')).toBeNull())
      // Going back is not a dismissal: the crewmate's chat still shows the result.
      expect(writes('/api/guide/dismiss')).toHaveLength(0)
      expect(endedBySlot(qc.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY) ?? [], new Set())['slot-radar']?.status)
        .toBe(how === 'cancel' ? 'cancelled' : 'completed')
    },
  )

  it('a guide from an ordinary chat goes back to that chat', async () => {
    await endOwnedGuide('slot-A', 'cancel')
    fireEvent.click(await screen.findByTestId('guide-back'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/chat?slot=slot-A'))
  })

  it('shows no way back while the user is already on that mate\'s chat', async () => {
    setViewedThreadSlot('slot-pilot')
    await endOwnedGuide('slot-pilot', 'complete', '/members?member=pilot')
    await act(async () => { await tickTracker(1) })
    expect(screen.queryByTestId('guide-back')).toBeNull()
    expect(screen.queryByTestId('guide-pill')).toBeNull()
  })

  it('the step panel BECOMES the finish chip: one element, never unmounted between', async () => {
    const owned = claimed(guide({ slot_key: 'slot-pilot' }))
    pending = [owned]
    renderGuide('/settings/display/theme', undefined, null)
    await screen.findByTestId('guide-continue')
    const panel = screen.getByTestId('guide-pill')
    const seen: Array<Element | null> = []
    const mo = new MutationObserver(() => seen.push(document.querySelector('[data-testid="guide-pill"]')))
    mo.observe(document.body, { childList: true, subtree: true })
    act(() => applyGuideUpdate(qc, { ...owned, status: 'completed', revision: owned.revision + 1, finished_at: 10 }))
    await screen.findByTestId('guide-back')
    mo.disconnect()
    expect(screen.getByTestId('guide-pill')).toBe(panel)
    expect(seen.every(el => el === panel)).toBe(true)
  })

  it('names no destination until the roster says whose chat it was', () => {
    expect(guideReturnFor('slot-cap', undefined, false)).toBeNull()
    expect(guideReturnFor('slot-cap', undefined, true)).toEqual({ kind: 'chat', to: '/chat?slot=slot-cap' })
    expect(guideReturnFor('slot-cap', [{ ...pilotRow, slot_key: 'slot-cap' }], false)).toEqual({ kind: 'chat', to: '/members?member=pilot' })
  })

  it('reads a mate thread from its slot key, never the chat page, which does not show it', () => {
    expect(memberSlugOfSlot('member-radar')).toBe('radar')
    expect(memberSlugOfSlot('member-pilot.memory-pilot-v2')).toBe('pilot')
    for (const k of ['member-', 'member-Radar', 'member--x', 'xmember-radar', 'chat-1-2', 'slot-A']) expect(memberSlugOfSlot(k)).toBeNull()
    // Another crewmate's: by exact name when one row carries the slug.
    const oncall = { name: 'Oncall', kiro_agent: 'kirocrew', slug: 'oncall', slot_key: 'member-oncall.memory-oncall-v2', running: false }
    expect(guideReturnFor('member-oncall', [oncall, radarRow], false)).toEqual({ kind: 'chat', to: '/members?member=Oncall' })
    // ...by slug when the roster failed, or two rows share the lossy slug.
    expect(guideReturnFor('member-radar', undefined, true)).toEqual({ kind: 'chat', to: '/members?member=radar' })
    expect(guideReturnFor('member-oncall', [oncall, { ...oncall, name: 'oncall', slot_key: '' }], false)).toEqual({ kind: 'chat', to: '/members?member=oncall' })
    // ...and the roster's answer is awaited.
    expect(guideReturnFor('member-radar', undefined, false)).toBeNull()
    // An ordinary chat slot still goes to the chat page.
    expect(guideReturnFor('chat-1-2', undefined, true)).toEqual({ kind: 'chat', to: '/chat?slot=chat-1-2' })
  })

  it.each([
    ['the roster failed', () => HttpResponse.json({ error: 'boom' }, { status: 500 }), '/members?member=radar'],
    ['the binding moved on', () => HttpResponse.json({ members: [pilotRow, { ...radarRow, name: 'Radar', slot_key: 'member-radar.memory-radar-v2' }] }), '/members?member=Radar'],
  ])('a mate\'s guide goes back to that mate when %s', async (_why, members, to) => {
    server.use(http.get('/api/members', members))
    await endOwnedGuide('member-radar', 'complete')
    const back = await screen.findByTestId('guide-back')
    expect(back.textContent).toBe(i18nT('components.meetCrewmatesFlow.back_to_chat'))
    fireEvent.click(back)
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe(to))
  })

  it('announces the end in a status region that was there before it', async () => {
    const owned = claimed(guide({ slot_key: 'member-pilot' }))
    pending = [owned]
    renderGuide('/settings/display/theme', undefined, null)
    await screen.findByTestId('guide-continue')
    const status = screen.getByTestId('guide-finished-status')
    expect(status.getAttribute('role')).toBe('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toBe('')
    act(() => applyGuideUpdate(qc, { ...owned, status: 'cancelled', revision: owned.revision + 1, finished_at: 10 }))
    await screen.findByTestId('guide-back')
    expect(screen.getByTestId('guide-finished-status')).toBe(status)
    expect(status.textContent).toBe(L('finished_cancelled'))
    fireEvent.click(screen.getByTestId('guide-finished-close'))
    await waitFor(() => expect(status.textContent).toBe(''))
  })

  it('the finish chip\'s close and way back are 44px touch targets', async () => {
    await endOwnedGuide('member-pilot', 'complete')
    for (const el of [await screen.findByTestId('guide-back'), screen.getByTestId('guide-finished-close')]) {
      expect(el.className).toMatch(/\bmin-h-11\b/)
    }
    expect(screen.getByTestId('guide-finished-close').className).toMatch(/\bmin-w-11\b/)
  })

  /** A Settings row the settings.show guide points at, with a laid-out box. */
  function SettingRow() {
    const entry = SETTINGS_REGISTRY.find(e => e.id === 'chat.default-model')!
    return (
      <div
        data-setting-label={i18nT(entry.labelKey!)}
        ref={(el) => {
          if (el) el.getBoundingClientRect = () => ({ top: 300, left: 100, width: 80, height: 30, right: 180, bottom: 330, x: 100, y: 300, toJSON: () => ({}) }) as DOMRect
        }}
      >
        row
      </div>
    )
  }

  it('Done that finishes the guide moves focus to the way back; closing returns it to the page', async () => {
    const g = guide({ slot_key: 'member-pilot', actions: [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }] })
    let current = claimed(g)
    pending = [current]
    onWrite = (name) => {
      if (name === 'progress') current = { ...current, status: 'completed', revision: current.revision + 1, finished_at: 10 }
      return current
    }
    render(
      <Provider store={store}>
        <QueryClientProvider client={(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }))}>
          <MemoryRouter initialEntries={['/overview']}>
            <GuideProvider>
              <main id="main-content" tabIndex={-1}><GuideLayer /><SettingRow /><LocationProbe /></main>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByTestId('guide-continue'))
    const next = await screen.findByTestId('guide-next')
    await waitFor(() => expect((next as HTMLButtonElement).disabled).toBe(false))
    // The guide's only step: the button ends it, so it reads Done, not Next.
    expect(next.textContent).toBe(i18nT('components.meetCrewmatesFlow.done'))
    expect(next.textContent).not.toBe(L('next'))
    act(() => next.focus())
    fireEvent.click(next)
    const back = await screen.findByTestId('guide-back')
    await waitFor(() => expect(document.activeElement).toBe(back))
    act(() => screen.getByTestId('guide-finished-close').focus())
    fireEvent.click(screen.getByTestId('guide-finished-close'))
    await waitFor(() => expect(screen.queryByTestId('guide-pill')).toBeNull())
    expect(document.activeElement).toBe(document.getElementById('main-content'))
  })

  it('Done works again on a replayed guide', async () => {
    const g = guide({ slot_key: 'member-pilot', actions: [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }] })
    let current = claimed(g)
    pending = [current]
    onWrite = (name) => {
      if (name === 'progress') current = { ...current, status: 'completed', revision: current.revision + 1, finished_at: 10 }
      if (name === 'claim') current = { ...current, status: 'active', owner_tab: TAB_ID, revision: current.revision + 1 }
      return current
    }
    server.use(http.post('/api/guide/replay', async ({ request }) => {
      calls.push({ path: '/api/guide/replay', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      current = { ...current, status: 'offered', owner_tab: null, revision: current.revision + 1, finished_at: null, step_index: 0, action_index: 0 }
      return HttpResponse.json({ guide: current })
    }))
    render(
      <Provider store={store}>
        <QueryClientProvider client={(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }))}>
          <MemoryRouter initialEntries={['/overview']}>
            <GuideProvider>
              <main id="main-content" tabIndex={-1}>
                <GuideLayer /><SettingRow /><LocationProbe />
                <GuideOfferCard guideId={g.guide_id} slotKey="member-pilot" />
              </main>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByTestId('guide-continue'))
    const first = await screen.findByTestId('guide-next')
    await waitFor(() => expect((first as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(first)
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    fireEvent.click(await screen.findByTestId('guide-replay'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    const again = await screen.findByTestId('guide-next')
    await waitFor(() => expect((again as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(again)
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
  })

  it('a replayed multi-action guide enters each action\'s page again', async () => {
    const actions = [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }, { id: 'mcp.open_add', params: {} }]
    const g = guide({ slot_key: 'member-pilot', actions })
    let current: Guide = { ...claimed(g), action_index: 1, step_index: 0 }
    pending = [current]
    onWrite = (name) => {
      if (name === 'claim') current = { ...current, status: 'active', owner_tab: TAB_ID, revision: current.revision + 1, action_index: 0, step_index: 0 }
      return current
    }
    server.use(http.post('/api/guide/replay', async ({ request }) => {
      calls.push({ path: '/api/guide/replay', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      current = { ...current, status: 'offered', owner_tab: null, revision: current.revision + 1, finished_at: null, action_index: 0, step_index: 0 }
      return HttpResponse.json({ guide: current })
    }))
    render(
      <Provider store={store}>
        <QueryClientProvider client={(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }))}>
          <MemoryRouter initialEntries={['/overview']}>
            <GuideProvider>
              <main id="main-content" tabIndex={-1}>
                <GuideLayer /><SettingRow /><LocationProbe />
                <GuideOfferCard guideId={g.guide_id} slotKey="member-pilot" />
              </main>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    // The first walk entered the second action in this tab.
    fireEvent.click(await screen.findByTestId('guide-continue'))
    await waitFor(() => expect(screen.queryByTestId('guide-continue')).toBeNull())
    current = { ...current, status: 'completed', owner_tab: null, revision: current.revision + 1, finished_at: 10 }
    act(() => { qc.setQueryData(GUIDE_PENDING_QUERY_KEY, [current]) })
    fireEvent.click(await screen.findByTestId('guide-replay'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    // The gateway advances the replay to its second action.
    current = { ...current, action_index: 1, step_index: 0, revision: current.revision + 1 }
    act(() => { qc.setQueryData(GUIDE_PENDING_QUERY_KEY, [current]) })
    expect(await screen.findByTestId('guide-continue')).toBeTruthy()
  })

  it('a settings step another action follows keeps Next', async () => {
    const g = guide({ slot_key: 'member-pilot', actions: [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }, { id: 'mcp.open_add', params: {} }] })
    pending = [claimed(g)]
    onWrite = () => null
    render(
      <Provider store={store}>
        <QueryClientProvider client={(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }))}>
          <MemoryRouter initialEntries={['/overview']}>
            <GuideProvider>
              <main id="main-content" tabIndex={-1}><GuideLayer /><SettingRow /><LocationProbe /></main>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByTestId('guide-continue'))
    const next = await screen.findByTestId('guide-next')
    expect(next.textContent).toBe(L('next'))
  })

  it('Cancel moves focus into the chip; a guide that ends while the user works elsewhere does not steal focus', async () => {
    await endOwnedGuide('member-pilot', 'cancel')
    // Cancel was clicked without focusing it (a mouse on Safari): focus stays put.
    expect(document.activeElement).toBe(document.body)
    cleanup()

    const owned = claimed(guide({ slot_key: 'member-pilot' }))
    pending = [owned]
    onWrite = (name) => (name === 'cancel' ? { ...owned, status: 'cancelled', revision: owned.revision + 1, finished_at: 10 } : null)
    renderGuide('/settings/display/theme', undefined, null)
    await screen.findByTestId('guide-continue')
    const cancel = screen.getByRole('button', { name: L('cancel_guide') })
    act(() => cancel.focus())
    fireEvent.click(cancel)
    await screen.findByTestId('guide-back')
    // Focus lands in the chip: on the way back, or on the finish line when the
    // way back was not known yet as the guide ended (the roster loads async).
    const chip = document.querySelector('[data-placement="chip"]')
    await waitFor(() => expect(chip?.contains(document.activeElement)).toBe(true))
    expect(document.activeElement).not.toBe(document.body)
    cleanup()

    // Focus was in the panel, then the user pressed on the page (a spot that
    // takes no focus, so the blur names no new target).
    pending = [owned]
    renderGuide('/settings/display/theme', <p data-testid="page-text">text</p>, null)
    const cont = await screen.findByTestId('guide-continue')
    act(() => cont.focus())
    fireEvent.pointerDown(screen.getByTestId('page-text'))
    act(() => cont.blur())
    act(() => applyGuideUpdate(qc, { ...owned, status: 'completed', revision: owned.revision + 1, finished_at: 10 }))
    await screen.findByTestId('guide-back')
    await act(async () => { await tickTracker(1) })
    expect(document.activeElement).toBe(document.body)
  })
})

describe("the offering mate's own words in a guide", () => {
  // The guide's slot is a crewmate's pinned thread, so its words are that
  // crewmate's: the "From" line names it from the roster.
  beforeEach(() => {
    server.use(http.get('/api/members', () => HttpResponse.json({
      members: [{ name: 'radar-keeper', display_name: 'Skipper', kiro_agent: 'kirocrew', slug: 'radar-keeper', slot_key: 'slot-A', running: false }],
    })))
  })

  const noted = (over: Partial<Guide> = {}): Partial<Guide> => ({
    intro: 'Radar keeps an eye on your build so you do not have to.',
    actions: [{ ...MCP_OPEN, note: 'Adding it is what puts radar on duty tonight.' }],
    ...over,
  })
  const follows = (a: HTMLElement, b: HTMLElement) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

  it('shows the intro on the offer card under its title, attributed to the offering mate', async () => {
    pending = [guide(noted())]
    renderGuide('/chat/slot-A')
    const intro = await screen.findByTestId('guide-offer-intro')
    expect(intro.textContent).toContain('Radar keeps an eye on your build so you do not have to.')
    expect((await screen.findByTestId('guide-offer-intro-from')).textContent).toBe(L('note_from', { name: 'Skipper' }))
    const card = screen.getByTestId('guide-offer-card')
    expect(follows(card.querySelector('h3')!, intro)).toBe(true)
  })

  it('renders the text as text, never as markup', async () => {
    pending = [guide(noted({ intro: '<b>bold</b> <img src=x onerror=alert(1)>' }))]
    renderGuide('/chat/slot-A')
    const intro = await screen.findByTestId('guide-offer-intro')
    expect(intro.querySelector('b, img')).toBeNull()
    expect(intro.textContent).toContain('<b>bold</b> <img src=x onerror=alert(1)>')
  })

  it("puts the intro on the first step, under the dashboard's own line, and no note there", async () => {
    await startCrewmate(<Target anchor={GUIDE_ANCHORS.mcpServersTab} />, noted(), '/capabilities')
    const template = await screen.findByTestId('guide-step-text')
    expect(template.textContent).toBe(L('step_mcp_open_tab'))
    const intro = screen.getByTestId('guide-step-intro')
    expect(follows(template, intro)).toBe(true)
    expect((await screen.findByTestId('guide-step-intro-from')).textContent).toBe(L('note_from', { name: 'Skipper' }))
    expect(screen.queryByTestId('guide-step-note')).toBeNull()
  })

  it("shows an action's note under its final step and keeps the template line", async () => {
    await startCrewmate(<Target anchor={GUIDE_ANCHORS.mcpCustomJson} />, noted({ step_index: 2 }), '/capabilities')
    const template = await screen.findByTestId('guide-step-text')
    expect(template.textContent).toBe(L('step_mcp_custom_json'))
    const note = screen.getByTestId('guide-step-note')
    expect(note.textContent).toContain('Adding it is what puts radar on duty tonight.')
    expect(screen.getByTestId('guide-step-note-from').textContent).toBe(L('note_from', { name: 'Skipper' }))
    expect(follows(template, note)).toBe(true)
    expect(screen.queryByTestId('guide-step-intro')).toBeNull()
  })

  describe('a step that is both the first and an action\'s last', () => {
    const one = (note?: string): Guide => claimed(guide({
      intro: 'Find the Developer Mode setting',
      actions: [{ id: 'settings.show', params: { setting_id: 'chat.default-model' }, ...(note === undefined ? {} : { note }) }],
    }))
    const at = () => {
      const r = resolveGuideAction({ id: 'settings.show', params: { setting_id: 'chat.default-model' } })
      if (!r.ok) throw new Error(r.reason)
      return r.action.enter.to({ pathname: '/', search: '' })
    }

    it('shows one block, the note, never the intro beside it', async () => {
      pending = [one('This takes you right to the toggle.')]
      renderGuide(at(), undefined, null)
      fireEvent.click(await screen.findByTestId('guide-continue'))
      const note = await screen.findByTestId('guide-step-note')
      expect(note.textContent).toContain('This takes you right to the toggle.')
      expect(screen.queryByTestId('guide-step-intro')).toBeNull()
      expect(await screen.findAllByText(L('note_from', { name: 'Skipper' }))).toHaveLength(1)
    })

    it('falls back to the intro when the action has no note', async () => {
      pending = [one('  ')]
      renderGuide(at(), undefined, null)
      fireEvent.click(await screen.findByTestId('guide-continue'))
      const intro = await screen.findByTestId('guide-step-intro')
      expect(intro.textContent).toContain('Find the Developer Mode setting')
      expect(screen.queryByTestId('guide-step-note')).toBeNull()
    })
  })

  it('draws nothing extra when the agent gave no words', async () => {
    pending = [guide()]
    renderGuide('/chat/slot-A')
    await screen.findByTestId('guide-start')
    expect(screen.queryByTestId('guide-offer-intro')).toBeNull()
  })
})
