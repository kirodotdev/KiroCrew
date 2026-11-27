/**
 * Conductor mode Board > Status > Needs your input: each ledger item a worker said
 * waits on the PERSON and carries the conductor's current ask record shows what is
 * needed, why, the answers and the pick. A worker question with no current record
 * is the conductor's: it is not listed and not counted. A button fills the chat box and never sends.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(), sessionSummary: vi.fn(), sendMessage: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel, { personAsks } from './ConductorModeCrewPanel'
import { CrewWorkersTab } from './CrewWorkerList'
import type { WorkBoardItem } from '../../../api/crewBoard'
import { i18next } from '../../../i18n/all'
import zh from '../../../i18n/locales/zh-CN.json'

const ASK = { what: 'Pick the tea for the menu', why: 'Only you know the guests', options: [{ label: 'Oolong', outcome: 'ships today' }, { label: 'Green', outcome: '' }], pick: 'Oolong', pick_why: 'already stocked', written_at: '2026-10-30T00:06:00Z' }

/** A question the conductor vetted: it carries a current ask record. */
const ITEM = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_q', title: 'Pick a tea', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'question', summary: 'Which tea goes on the menu: oolong or green?', artifacts: {}, pr: null,
  last_report_at: '2026-10-30T00:05:00Z', created_at: '2026-10-30T00:00:00Z', closed_at: null, decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: true, terminal: false, alive: 'idle', events: [],
  reason: 'needs_human', awaits_person: true, ask: ASK, ...over,
} as WorkBoardItem)

const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Plan the launch.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', items: [] as WorkBoardItem[], take_over_available: false }


function renderWith(ui: React.ReactElement, slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  const view = render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  return { store, qc, view }
}

describe('Conductor mode Needs your input', () => {
  beforeEach(() => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => vi.clearAllMocks())

  it('a worker question with no current ask record is the conductor\'s: not listed, not counted', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ ask: undefined, awaits_person: false })], asks: { it_q: ['Oolong'] } })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    expect(await screen.findByTestId('conductor-mode-milestone')).toBeInTheDocument()
    expect(screen.queryByTestId('conductor-mode-person-ask')).toBeNull()
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveTextContent('· 0')
    expect(screen.getByTestId('conductor-mode-needs-you-none')).toHaveTextContent('Nothing needs you right now.')
    expect(screen.queryByText('Which tea goes on the menu: oolong or green?', { selector: '[data-testid="conductor-mode-needs-you"] *' })).toBeNull()
    // Its milestone is not the person's, and the recap does not say it waits on you.
    expect(screen.getByTestId('conductor-mode-milestone')).not.toHaveAttribute('data-state', 'needs_you')
    expect(screen.getByTestId('conductor-mode-recap')).not.toHaveTextContent('waits on you')
  })

  it('with an ask record: what, why, one button per answer with its outcome, the pick marked, Reply secondary', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    const row = await screen.findByTestId('conductor-mode-person-ask')
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveTextContent('· 1')
    expect(row).not.toHaveTextContent('Which tea goes on the menu')
    expect(within(row).getByTestId('conductor-mode-person-ask-what')).toHaveTextContent('Pick the tea for the menu')
    expect(within(row).getByTestId('conductor-mode-person-ask-why')).toHaveTextContent('Why only you: Only you know the guests')
    const options = within(row).getAllByTestId('conductor-mode-person-ask-option')
    expect(options.map(o => o.tagName)).toEqual(['BUTTON', 'BUTTON'])
    expect(options[0]).toHaveTextContent('OolongRecommendedships today')
    expect(options[0]).toHaveAttribute('data-pick', 'yes')
    expect(options[1]).toHaveTextContent('Green')
    expect(options[1]).not.toHaveAttribute('data-pick')
    expect(within(row).getAllByTestId('conductor-mode-person-ask-pick')).toHaveLength(1)
    expect(within(row).getByTestId('conductor-mode-person-ask-pick-why')).toHaveTextContent('Why recommended: already stocked')
    expect(within(row).getByTestId('conductor-mode-person-ask-reply')).toHaveTextContent('Write a reply')
    // The visible line says the buttons send nothing.
    expect(within(row).getByTestId('conductor-mode-person-ask-hint')).toHaveTextContent('Pick an answer to draft it in your chat box.')
    expect(screen.getByTestId('conductor-mode-milestone')).toHaveTextContent('Waiting on your answer above')
  })

  it('a button fills the chat box with the item reference and never sends', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    const onAct = vi.fn()
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={onAct} />)
    fireEvent.click((await screen.findAllByTestId('conductor-mode-person-ask-option'))[0])
    expect(onAct).toHaveBeenLastCalledWith('About "Pick a tea" (it_q): Oolong')
    fireEvent.click(screen.getByTestId('conductor-mode-person-ask-reply'))
    expect(onAct).toHaveBeenLastCalledWith('About "Pick a tea" (it_q): ')
    expect(api.sendMessage).not.toHaveBeenCalled()
  })

  it('options without a pick: no answer is marked', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ ask: { ...ASK, pick: '', pick_why: '' } })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    expect(await screen.findAllByTestId('conductor-mode-person-ask-option')).toHaveLength(2)
    expect(screen.queryByTestId('conductor-mode-person-ask-pick')).toBeNull()
    expect(screen.queryByTestId('conductor-mode-person-ask-pick-why')).toBeNull()
  })

  it("a record with no options borrows goal-board's offered labels; with neither, Reply alone", async () => {
    const bare = { ...ASK, options: [], pick: '', pick_why: '' }
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ ask: bare })], asks: { it_q: ['Oolong', 'Green'] } })
    const { qc } = renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    expect((await screen.findAllByTestId('conductor-mode-person-ask-option')).map(b => b.textContent)).toEqual(['Oolong', 'Green'])
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ ask: bare })] })
    await qc.invalidateQueries()
    await waitFor(() => expect(screen.queryByTestId('conductor-mode-person-ask-option')).toBeNull())
    expect(screen.getByTestId('conductor-mode-person-ask-reply')).toBeInTheDocument()
  })

  it('a newer report (the board drops the stale record) takes the item off the list', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    const { qc } = renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    await screen.findByTestId('conductor-mode-person-ask-what')
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ ask: undefined, awaits_person: false, summary: 'Also the cups?' })] })
    await qc.invalidateQueries()
    await waitFor(() => expect(screen.queryByTestId('conductor-mode-person-ask')).toBeNull())
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveTextContent('· 0')
  })

  it('reads in Chinese: the ask record labels', async () => {
    await i18next.changeLanguage('zh-CN')
    try {
      api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
      renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
      const crew = zh.components.crewMode
      expect(await screen.findByTestId('conductor-mode-person-ask-why')).toHaveTextContent(crew.conductor_mode_ask_why.replace('{{why}}', 'Only you know the guests'))
      expect(screen.getByTestId('conductor-mode-person-ask-pick')).toHaveTextContent(crew.conductor_mode_ask_pick)
      expect(screen.getByTestId('conductor-mode-person-ask-pick-why')).toHaveTextContent(crew.conductor_mode_ask_pick_why.replace('{{why}}', 'already stocked'))
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('says nothing waits when nothing does', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ status: 'progress', reason: null, outstanding: false, awaits_person: false })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    expect(await screen.findByTestId('conductor-mode-milestone')).toBeInTheDocument()
    expect(screen.getByTestId('conductor-mode-needs-you-none')).toBeInTheDocument()
    expect(screen.queryByTestId('conductor-mode-person-ask')).toBeNull()
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveTextContent('· 0')
  })

  it('an item leaves once answered', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    const { qc } = renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    await screen.findByTestId('conductor-mode-person-ask')
    // Answered (a decision, or the person's steer): the board read stops flagging it.
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ decided_at: '2026-10-30T00:06:00Z', outstanding: false, awaits_person: false })] })
    await qc.invalidateQueries()
    expect(await screen.findByTestId('conductor-mode-needs-you-none')).toBeInTheDocument()
    expect(screen.queryByTestId('conductor-mode-person-ask')).toBeNull()
  })

  it('with a crew member on screen the item shows its answers read-only and offers to open the main chat', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    const { store } = renderWith(<ConductorModeCrewPanel root="root-1" viewing="w-1" active onAct={vi.fn()} />, [{ key: 'root-1', title: 'Root', crew_mode_root: true, messages: 2 }, { key: 'w-1', title: 'Worker', crew_root: 'root-1' }])
    const row = await screen.findByTestId('conductor-mode-person-ask')
    expect(row).toHaveTextContent('Pick a tea')
    expect(within(row).queryByTestId('conductor-mode-person-ask-reply')).toBeNull()
    expect(within(row).getAllByTestId('conductor-mode-person-ask-option').map(o => o.tagName)).toEqual(['DIV', 'DIV'])
    expect(within(row).getByTestId('conductor-mode-person-ask-open-main')).toHaveTextContent('Back to main chat')
    fireEvent.click(within(row).getByTestId('conductor-mode-person-ask-open-main'))
    expect(store.getState().chat.activeSlot).toBe('root-1')
  })

  it('with the main chat closed, the row reopens it from its saved transcript', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    api.resumeChatSlot.mockResolvedValue({ ok: false })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="w-1" active onAct={vi.fn()} />, [{ key: 'w-1', title: 'Worker', crew_root: 'root-1' }])
    const row = await screen.findByTestId('conductor-mode-person-ask')
    expect(within(row).getByTestId('conductor-mode-person-ask-open-main')).toHaveTextContent('Open main chat')
    fireEvent.click(within(row).getByTestId('conductor-mode-person-ask-open-main'))
    expect(api.resumeChatSlot).toHaveBeenCalled()
  })

  it('a refused skip (a newer ask arrived) says so on the card and keeps it', async () => {
    const report = { id: 'r1', ts: '2026-10-30T00:05:00Z', item_id: 'it_q', kind: 'report', status: 'question', text: '' }
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ events: [report] as never })] })
    api.crewBoardAction.mockRejectedValue(new Error('Nothing was skipped: this ask changed or was answered since the page loaded.'))
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    fireEvent.click(await screen.findByTestId('conductor-mode-person-ask-dismiss'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was skipped: this ask changed or was answered since the page loaded.')
    expect(screen.getByTestId('conductor-mode-person-ask')).toBeInTheDocument()
  })

  it('"Skip question" dismisses the ask it shows on the server and the card leaves', async () => {
    const report = { id: 'r1', ts: '2026-10-30T00:05:00Z', item_id: 'it_q', kind: 'report', status: 'question', text: '' }
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ events: [report] as never })] })
    api.crewBoardAction.mockImplementation(async () => {
      // The server wrote the person's steer: the next read no longer flags it.
      api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ awaits_person: false, outstanding: false, events: [{ id: 'e1', ts: '2026-10-30T00:06:00Z', item_id: 'it_q', kind: 'steer', status: null, text: '' }] as never })] })
      return { ok: true, action: 'dismiss', item_id: 'it_q' }
    })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    const close = await screen.findByRole('button', { name: 'Skip "Pick a tea" without replying' })
    expect(close).toHaveTextContent('Skip question')
    // What skipping does is on the card, not only in a tooltip.
    expect(screen.getByTestId('conductor-mode-person-ask-dismiss-note')).toHaveTextContent('It comes back only if it asks again.')
    fireEvent.click(close)
    await waitFor(() => expect(api.crewBoardAction).toHaveBeenCalledWith('root-1', 'it_q', 'dismiss', 'r1'))
    expect(await screen.findByTestId('conductor-mode-needs-you-none')).toBeInTheDocument()
    expect(screen.queryByTestId('conductor-mode-person-ask')).toBeNull()
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveTextContent('· 0')
  })

  it('a worker running again is working, not waiting on you', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ alive: 'running', awaits_person: false })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    expect(await screen.findByTestId('conductor-mode-milestone')).toHaveAttribute('data-state', 'running')
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveTextContent('· 0')
  })

  it('the Board tab hands its composer insert to the panel', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    const onAct = vi.fn()
    renderWith(<CrewWorkersTab slot="root-1" active onAct={onAct} />, [{ key: 'root-1', title: 'Root' }])
    fireEvent.click(await screen.findByTestId('conductor-mode-person-ask-reply'))
    expect(onAct).toHaveBeenCalledWith('About "Pick a tea" (it_q): ')
  })
})

describe('personAsks', () => {
  it("reads the board's own awaits_person flag", () => {
    expect(personAsks([ITEM({})])).toHaveLength(1)
    expect(personAsks([ITEM({ awaits_person: false })])).toHaveLength(0)
    // An older gateway sends no flag: nothing is listed rather than guessed.
    expect(personAsks([ITEM({ awaits_person: undefined })])).toHaveLength(0)
    // A worker question the conductor has not vetted is not the person's.
    expect(personAsks([ITEM({ ask: undefined })])).toHaveLength(0)
  })

  it('leaves an item whose tool approval already has its own card', () => {
    expect(personAsks([ITEM({ reason: 'approval', status: 'blocked' })], new Set(['it_q']))).toHaveLength(0)
  })
})
