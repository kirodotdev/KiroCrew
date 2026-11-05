/**
 * Ultra Mode Board > Status > Needs your input: each ledger item a worker said
 * waits on the PERSON (goal-board's "needs you" rule) shows its task and what it
 * asks, with goal-board's Reply buttons -- the conductor's offered answers from the
 * board read's `asks`. A button fills the chat box and never sends.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), sessionSummary: vi.fn(), sendMessage: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import UltraCrewPanel, { personAsks } from './UltraCrewPanel'
import { CrewWorkersTab } from './CrewWorkerList'
import type { WorkBoardItem } from '../../../api/crewBoard'
import { crewModePreviewOn } from '../../../test/crewModePreview'

const ITEM = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_q', title: 'Pick a tea', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'question', summary: 'Which tea goes on the menu: oolong or green?', artifacts: {}, pr: null,
  last_report_at: '2026-10-30T00:05:00Z', created_at: '2026-10-30T00:00:00Z', closed_at: null, decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: true, terminal: false, alive: 'idle', events: [],
  reason: 'needs_human', awaits_person: true, ...over,
} as WorkBoardItem)

const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Plan the launch.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', items: [] as WorkBoardItem[], take_over_available: false }

/** A Radix menu trigger opens on a mouse pointerdown. */
function openMenu(el: HTMLElement) {
  fireEvent.pointerDown(el, { pointerType: 'mouse', button: 0, ctrlKey: false })
}

function renderWith(ui: React.ReactElement, slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  const view = render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  return { store, qc, view }
}

describe('Ultra Mode Needs your input', () => {
  beforeEach(() => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => vi.clearAllMocks())

  it('shows a waiting item: which task and what it asks, counted', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    const row = await screen.findByTestId('ultra-person-ask')
    expect(row).toHaveTextContent('Pick a tea')
    expect(within(row).getByTestId('ultra-person-ask-text')).toHaveTextContent('Which tea goes on the menu: oolong or green?')
    expect(screen.getByTestId('ultra-needs-you')).toHaveTextContent('· 1')
    expect(screen.queryByTestId('ultra-needs-you-none')).toBeNull()
    // Its milestone and the recap say it waits on the person, not on the main chat.
    expect(screen.getByTestId('ultra-milestone')).toHaveTextContent('Waiting on your answer above')
    expect(screen.getByTestId('ultra-recap')).toHaveTextContent('Pick a tea waits on you')
  })

  it("puts the conductor's offered answers in one menu beside Reply: two controls in the row", async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})], asks: { it_q: ['Oolong', 'Green'] } })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    const row = await screen.findByTestId('ultra-person-ask')
    const actions = within(row).getByTestId('ultra-person-ask-actions')
    expect(within(actions).getAllByRole('button')).toHaveLength(2)
    expect(within(row).getByTestId('ultra-person-ask-reply')).toHaveTextContent('Write a reply')
    // The visible line says the buttons send nothing.
    expect(within(row).getByTestId('ultra-person-ask-hint')).toHaveTextContent('Puts this in your chat box to check and send')
    openMenu(within(row).getByTestId('ultra-person-ask-answers'))
    expect((await screen.findAllByTestId('ultra-person-ask-option')).map(b => b.textContent)).toEqual(['Oolong', 'Green'])
  })

  it('a button fills the chat box with the item reference and never sends', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})], asks: { it_q: ['Oolong'] } })
    const onAct = vi.fn()
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active onAct={onAct} />)
    openMenu(await screen.findByTestId('ultra-person-ask-answers'))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Oolong' }))
    expect(onAct).toHaveBeenLastCalledWith('About "Pick a tea" (it_q): Oolong')
    fireEvent.click(screen.getByTestId('ultra-person-ask-reply'))
    expect(onAct).toHaveBeenLastCalledWith('About "Pick a tea" (it_q): ')
    expect(api.sendMessage).not.toHaveBeenCalled()
  })

  it('no offered answers: Reply alone, no menu', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    await screen.findByTestId('ultra-person-ask-reply')
    expect(screen.queryByTestId('ultra-person-ask-answers')).toBeNull()
  })

  it('says nothing waits when nothing does', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ status: 'progress', reason: null, outstanding: false, awaits_person: false })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    expect(await screen.findByTestId('ultra-milestone')).toBeInTheDocument()
    expect(screen.getByTestId('ultra-needs-you-none')).toBeInTheDocument()
    expect(screen.queryByTestId('ultra-person-ask')).toBeNull()
    expect(screen.getByTestId('ultra-needs-you')).toHaveTextContent('· 0')
  })

  it('an item leaves once answered', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    const { qc } = renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} />)
    await screen.findByTestId('ultra-person-ask')
    // Answered (a decision, or the person's steer): the board read stops flagging it.
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ decided_at: '2026-10-30T00:06:00Z', outstanding: false, awaits_person: false })] })
    await qc.invalidateQueries()
    expect(await screen.findByTestId('ultra-needs-you-none')).toBeInTheDocument()
    expect(screen.queryByTestId('ultra-person-ask')).toBeNull()
  })

  it('with a crew member on screen the item shows and offers to open the main chat', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})], asks: { it_q: ['Oolong'] } })
    const { store } = renderWith(<UltraCrewPanel root="root-1" viewing="w-1" active onAct={vi.fn()} />, [{ key: 'root-1', title: 'Root', crew_mode_root: true, messages: 2 }, { key: 'w-1', title: 'Worker', crew_root: 'root-1' }])
    const row = await screen.findByTestId('ultra-person-ask')
    expect(row).toHaveTextContent('Pick a tea')
    expect(within(row).queryByTestId('ultra-person-ask-reply')).toBeNull()
    expect(within(row).getByTestId('ultra-person-ask-open-main')).toHaveTextContent('Back to main chat')
    fireEvent.click(within(row).getByTestId('ultra-person-ask-open-main'))
    expect(store.getState().chat.activeSlot).toBe('root-1')
  })

  it('with the main chat closed, the row reopens it from its saved transcript', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    api.resumeChatSlot.mockResolvedValue({ ok: false })
    renderWith(<UltraCrewPanel root="root-1" viewing="w-1" active onAct={vi.fn()} />, [{ key: 'w-1', title: 'Worker', crew_root: 'root-1' }])
    const row = await screen.findByTestId('ultra-person-ask')
    expect(within(row).getByTestId('ultra-person-ask-open-main')).toHaveTextContent('Open main chat')
    fireEvent.click(within(row).getByTestId('ultra-person-ask-open-main'))
    expect(api.resumeChatSlot).toHaveBeenCalled()
  })

  it('the Board tab hands its composer insert to the panel', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    const onAct = vi.fn()
    renderWith(<CrewWorkersTab slot="root-1" active onAct={onAct} />, [{ key: 'root-1', title: 'Root' }])
    fireEvent.click(await screen.findByTestId('ultra-person-ask-reply'))
    expect(onAct).toHaveBeenCalledWith('About "Pick a tea" (it_q): ')
  })
})

describe('personAsks', () => {
  it("reads the board's own awaits_person flag", () => {
    expect(personAsks([ITEM({})])).toHaveLength(1)
    expect(personAsks([ITEM({ awaits_person: false })])).toHaveLength(0)
    // An older gateway sends no flag: nothing is listed rather than guessed.
    expect(personAsks([ITEM({ awaits_person: undefined })])).toHaveLength(0)
  })

  it('leaves an item whose tool approval already has its own card', () => {
    expect(personAsks([ITEM({ reason: 'approval', status: 'blocked' })], new Set(['it_q']))).toHaveLength(0)
  })
})

// Last, after the file's own hooks: Ultra Mode is a feature preview.
crewModePreviewOn()
