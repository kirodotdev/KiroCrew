/**
 * Conductor mode Board > Milestones: an item whose worker hosts a discussion
 * reads "Discussing" with its round and minutes, the agreed / disputed /
 * checking counts under it, and behind a toggle what the discussion holds.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(), sessionSummary: vi.fn(), sendMessage: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel from './ConductorModeCrewPanel'
import type { WorkBoardItem } from '../../../api/crewBoard'

const TALK = {
  phase: 'talking' as const, round: 2, minutes: 15, direction: 'Card multiline first',
  agreed: ['One send carries text and files', 'Offline says so'], checking: ['Paste size cap'],
  disputed: [{ point: 'Attach button', sides: 'A: in the card · B: in the chat' }],
  recent: [{ who: 'A', text: 'The card already has a box' }], thread: '/chat/abc',
}

const ITEM = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_a', title: 'Pick the card input', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'progress', summary: 'Round 2 running.', artifacts: {}, pr: null,
  last_report_at: '2026-11-18T18:30:00Z', created_at: '2026-11-18T18:00:00Z', closed_at: null, decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: false, alive: 'running', events: [],
  reason: null, awaits_person: false, discussion: TALK, ...over,
} as WorkBoardItem)

const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Pick the input.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', items: [] as WorkBoardItem[], take_over_available: false }

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots: [] } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-1" viewing="root-1" active /></Provider></QueryClientProvider>)
}

async function row() {
  return (await screen.findAllByTestId('conductor-mode-milestone'))[0]
}

describe('Conductor mode Board: the discussing row', () => {
  beforeEach(() => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => vi.clearAllMocks())

  it('shows Discussing with round, minutes and counts, and the panel behind a toggle', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    renderPanel()
    const r = await row()
    expect(r).toHaveAttribute('data-state', 'discussing')
    expect(within(r).getByTestId('conductor-mode-discussing')).toHaveTextContent('Discussing · Round 2 · 15')
    expect(within(r).getByTestId('conductor-mode-talk-counts')).toHaveTextContent('Agreed 2 · Disputed 1 · Being checked 1')
    const panel = within(r).getByTestId('conductor-mode-talk-panel')
    expect(panel).not.toBeVisible()
    const toggle = within(r).getByTestId('conductor-mode-talk-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveTextContent('Hide the discussion')
    expect(panel).toBeVisible()
    for (const text of ['Direction', 'Card multiline first', 'Offline says so', 'Attach button', 'A: in the card · B: in the chat', 'Paste size cap', 'The card already has a box']) {
      expect(panel).toHaveTextContent(text)
    }
    // An older report (no rounds) keeps its latest lines, and nothing opens a new page.
    expect(panel).toHaveTextContent('Latest lines')
    expect(within(panel).queryByTestId('conductor-mode-talk-rounds')).toBeNull()
    expect(r.querySelector('a[target="_blank"]')).toBeNull()
  })

  it('shows the talk round by round in place, oldest first, with no link out', async () => {
    const rounds = [
      { round: 2, lines: [{ who: 'A', text: 'Concedes: two inputs confuse' }, { who: 'B', text: 'Keeps the chat box' }] },
      { round: 1, lines: [{ who: 'A', text: 'The card owns its input' }, { who: 'B', text: 'Reuse the chat box' }, { who: '', text: '' }] },
    ]
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ discussion: { ...TALK, rounds } })] })
    renderPanel()
    const r = await row()
    fireEvent.click(within(r).getByTestId('conductor-mode-talk-toggle'))
    const panel = within(r).getByTestId('conductor-mode-talk-panel')
    expect(panel).toHaveTextContent('Round by round')
    const shown = within(panel).getAllByTestId('conductor-mode-talk-round')
    expect(shown.map(el => el.textContent)).toEqual([
      'Round 1A: The card owns its inputB: Reuse the chat box',
      'Round 2A: Concedes: two inputs confuseB: Keeps the chat box',
    ])
    // The rounds replace the latest lines, which would repeat the last round.
    expect(panel).not.toHaveTextContent('Latest lines')
    expect(r.querySelector('a')).toBeNull()
  })

  it('marks a running talk live, with a label, and only while talking or checking', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})] })
    renderPanel()
    const r = await row()
    const dot = within(r).getByRole('img', { name: 'Discussion in progress' })
    expect(dot).toHaveAttribute('data-testid', 'conductor-mode-talk-live')
    expect(dot.className).toContain('motion-safe:animate-pulse')
  })

  it('shows no live mark when the talk is done or its host is idle', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [
      ITEM({ item_id: 'it_done', discussion: { ...TALK, phase: 'done', round: 3 } }),
      ITEM({ item_id: 'it_idle', alive: 'idle' }),
    ] })
    renderPanel()
    const rows = await screen.findAllByTestId('conductor-mode-milestone')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByTestId('conductor-mode-discussing')).toHaveTextContent('Talk finished · Round 3')
    for (const r of rows) expect(within(r).queryByTestId('conductor-mode-talk-live')).toBeNull()
  })

  it('is gone once the item closes', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ state: 'accepted', verdict: 'pass', terminal: true, alive: 'closed', status: 'done', closed_at: '2026-11-18T19:00:00Z' })] })
    renderPanel()
    const r = await row()
    expect(r).toHaveAttribute('data-state', 'done')
    expect(within(r).queryByTestId('conductor-mode-talk')).toBeNull()
  })

  it('falls back to Quiet when its worker goes stale', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ stale: true, alive: 'idle' })] })
    renderPanel()
    const r = await row()
    expect(r).toHaveAttribute('data-state', 'quiet')
    expect(within(r).queryByTestId('conductor-mode-talk')).toBeNull()
    expect(within(r).queryByTestId('conductor-mode-talk-live')).toBeNull()
  })
})
