import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, within, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import AssistantMessage from '../pages/chat/AssistantMessage'
import { EngineSubagentLiveCard } from '../pages/chat/EngineSubagentRecord'
import { sseSubagentDone, sseSubagentSnapshot, sseSubagentSpawn } from '../store/chatSlice'
import { createTestStore } from './helpers'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

/**
 * A turn's KAS engine sub-agents are drawn as the card a `spawn_run` wave's
 * completion is (`CompletionCardFrame`): live at the foot of the transcript while
 * the turn runs, then from the reply's `meta.engine_subagents`. KAS reports no
 * credits, model or time for one child, so the card shows Kiro Crew's own clock and
 * only the turn's credit total, labelled as the whole turn's.
 */
const BASE = { content: 'Both ran.', isStreaming: false, slotRunning: false, messageTs: '2026-11-24T08:00:00Z' }
const child = (task: string, started: number, ended: number) => ({
  agent: 'kirocrew-helper', task, end: 'completed', tools: ['Run echo'], tool_count: 1, started_at: started, ended_at: ended,
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the reply\'s engine batch card', () => {
  it('shows the batch duration from first start to last end, and each child\'s', () => {
    render(<AssistantMessage {...BASE} engineSubagents={{ children: [child('one', 1000, 1003.2), child('two', 1000.5, 1004)] }} />)
    expect(screen.getByTestId('engine-batch-duration')).toHaveTextContent('4.0s')
    fireEvent.click(screen.getByTestId('engine-subagent-record-toggle'))
    expect(screen.getAllByTestId('engine-record-ran').map(n => n.textContent)).toEqual(['3.2s', '3.5s'])
  })

  it('shows only the turn\'s credits, labelled as the whole turn, never a per-child figure', () => {
    render(<AssistantMessage {...BASE} engineSubagents={{ children: [child('one', 1, 2), { ...child('two', 1, 2), credits: 9, model: 'x-model' }], turn_credits: 0.1874 }} />)
    const chip = screen.getByTestId('engine-batch-turn-credits')
    expect(chip).toHaveTextContent('0.19 credits, whole turn')
    expect(chip).toHaveAttribute('title', expect.stringMatching(/whole turn, sub-agents included/))
    expect(screen.getAllByText(/credits/)).toHaveLength(1)
    expect(screen.queryByTestId('subagent-completion-model')).toBeNull()
    expect(screen.getByTestId('engine-subagent-record')).not.toHaveTextContent('x-model')
  })

  it('counts and times the whole batch, the children left out of the list too', () => {
    const listed = Array.from({ length: 32 }, (_, i) => child(`t${i}`, 1000, 1002))
    render(<AssistantMessage {...BASE} engineSubagents={{
      children: listed, more: 1, ends: { completed: 32, failed: 1, not_reported: 0 }, started_at: 1000, ended_at: 1100,
    }} />)
    const card = screen.getByTestId('engine-subagent-record')
    expect(card).toHaveTextContent('33 of 33 subagents finished')
    expect(within(card).getByTestId('chip-ok')).toHaveTextContent('32')
    expect(within(card).getByTestId('chip-failed')).toHaveTextContent('1')
    expect(screen.getByTestId('engine-batch-duration')).toHaveTextContent('1m 40s')
    // A failure opens the card, as the spawn_run card does.
    expect(screen.getByTestId('engine-subagent-record-toggle')).toHaveAttribute('aria-expanded', 'true')
  })

  it('shows no duration or credits it was not given', () => {
    render(<AssistantMessage {...BASE} engineSubagents={{ children: [{ agent: 'a', task: 't', end: 'completed', tools: [], tool_count: 0 }] }} />)
    expect(screen.queryByTestId('engine-batch-duration')).toBeNull()
    expect(screen.queryByTestId('engine-batch-turn-credits')).toBeNull()
  })

  it('opens the Subagents panel from Panel, only where the host has one', () => {
    const open = vi.fn()
    const rec = { children: [child('one', 1, 2)] }
    const { unmount } = render(<AssistantMessage {...BASE} engineSubagents={rec} onOpenSubagentPanel={open} />)
    fireEvent.click(within(screen.getByTestId('engine-subagent-record')).getByRole('button', { name: 'Open in the Subagents panel' }))
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ kind: 'batch', total: 1 }))
    unmount()
    render(<AssistantMessage {...BASE} engineSubagents={rec} />)
    expect(within(screen.getByTestId('engine-subagent-record')).queryByRole('button', { name: 'Open in the Subagents panel' })).toBeNull()
  })
})

describe('the live engine batch card', () => {
  const spawn = (id: string, engine: boolean, turn = 'a1') =>
    sseSubagentSpawn({ slot: 'chat-1', id, task: `task ${id}`, agent: 'kirocrew-helper', ...(engine ? { engine: true, engine_turn: turn } : {}) })

  it('spins while a child runs, then says N of N finished, while the turn runs', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-24T08:00:00Z'))
    const store = createTestStore()
    const view = render(<Provider store={store}><EngineSubagentLiveCard slot="chat-1" running /></Provider>)
    expect(screen.queryByTestId('engine-subagent-live')).toBeNull()
    act(() => { store.dispatch(spawn('native:a', true)); store.dispatch(spawn('native:b', true)) })
    const card = screen.getByTestId('engine-subagent-live')
    expect(card).toHaveAttribute('data-running', 'true')
    expect(screen.getByTestId('glyph-running')).toHaveClass('animate-spin')
    expect(card).toHaveTextContent('2 agents running')
    act(() => { vi.advanceTimersByTime(3000) })
    expect(screen.getByTestId('engine-batch-duration')).toHaveTextContent('3s')
    act(() => {
      store.dispatch(sseSubagentDone({ slot: 'chat-1', id: 'native:a', elapsed: 3, engine: true, engine_turn: 'a1' }))
      store.dispatch(sseSubagentDone({ slot: 'chat-1', id: 'native:b', elapsed: 3, error: 'failed (the engine gave no reason)', engine: true, engine_turn: 'a1' }))
    })
    const done = screen.getByTestId('engine-subagent-live')
    expect(done).not.toHaveAttribute('data-running')
    expect(done).toHaveTextContent('2 of 2 subagents finished')
    expect(within(done).getByTestId('chip-ok')).toHaveTextContent('1')
    expect(within(done).getByTestId('chip-failed')).toHaveTextContent('1')
    view.rerender(<Provider store={store}><EngineSubagentLiveCard slot="chat-1" running={false} /></Provider>)
    expect(screen.queryByTestId('engine-subagent-live')).toBeNull()
  })

  it('draws nothing for a turn with no engine sub-agent (the preview off)', () => {
    const store = createTestStore()
    store.dispatch(spawn('abc123', false))
    store.dispatch(spawn('native:kiro', false))
    const { container } = render(<Provider store={store}><EngineSubagentLiveCard slot="chat-1" running /></Provider>)
    expect(container).toBeEmptyDOMElement()
  })

  it('leaves out an engine child of an earlier turn, even one that ended a moment ago', () => {
    const store = createTestStore()
    store.dispatch(spawn('native:old', true, 'a1'))
    store.dispatch(sseSubagentDone({ slot: 'chat-1', id: 'native:old', elapsed: 1, engine: true, engine_turn: 'a1' }))
    render(<Provider store={store}><EngineSubagentLiveCard slot="chat-1" running /></Provider>)
    // The new turn ran no engine sub-agent yet: the earlier batch is not drawn.
    expect(screen.queryByTestId('engine-subagent-live')).toBeNull()
    act(() => { store.dispatch(spawn('native:new', true, 'b2')) })
    const card = screen.getByTestId('engine-subagent-live')
    expect(card).toHaveTextContent('1 agent running')
    fireEvent.click(screen.getByTestId('engine-subagent-live-toggle'))
    expect(screen.getAllByTestId('engine-record-child')).toHaveLength(1)
  })

  it('a page opened mid-turn lists the turn\'s ended sub-agent beside the running one', () => {
    const store = createTestStore()
    // What a reconnect replays: one card of this turn ended, one still running.
    store.dispatch(sseSubagentSnapshot({ id: 'native:b', slot: 'chat-1', task: 'two', agent: 'kirocrew-helper', streaming: '', last_tool: '', started: 1000, engine: true, engine_turn: 'c3' }))
    store.dispatch(sseSubagentDone({ slot: 'chat-1', id: 'native:a', elapsed: 2, engine: true, engine_turn: 'c3', task: 'one', agent: 'kirocrew-helper' }))
    render(<Provider store={store}><EngineSubagentLiveCard slot="chat-1" running /></Provider>)
    const card = screen.getByTestId('engine-subagent-live')
    expect(card).toHaveTextContent('1 agent running')
    fireEvent.click(screen.getByTestId('engine-subagent-live-toggle'))
    expect(screen.getAllByTestId('engine-record-child').map(r => r.getAttribute('data-end')).sort()).toEqual(['completed', 'running'])
  })
})

describe('the live card across a queued next turn', () => {
  it('ends the earlier batch when the slot\'s turn identity moves with no pause in running', () => {
    const store = createTestStore()
    store.dispatch(sseSubagentSpawn({ slot: 'chat-1', id: 'native:a', task: 'one', agent: 'kirocrew-helper', engine: true, engine_turn: 'a1' }))
    const view = render(<Provider store={store}><EngineSubagentLiveCard slot="chat-1" running turnMark="g:1" /></Provider>)
    act(() => { store.dispatch(sseSubagentDone({ slot: 'chat-1', id: 'native:a', elapsed: 1, engine: true, engine_turn: 'a1' })) })
    // Still this turn: its ended batch stays.
    expect(screen.getByTestId('engine-subagent-live')).toHaveTextContent('1 of 1 subagents finished')
    view.rerender(<Provider store={store}><EngineSubagentLiveCard slot="chat-1" running turnMark="g:2" /></Provider>)
    expect(screen.queryByTestId('engine-subagent-live')).toBeNull()
  })
})

describe('a replayed engine card\'s start', () => {
  it('a done frame that names the gateway start keeps it, so the batch duration reads true after a reload', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(1_100_000))
    const store = createTestStore()
    store.dispatch(sseSubagentDone({ slot: 'chat-1', id: 'native:a', elapsed: 5, engine: true, engine_turn: 'd4', started: 1000 }))
    store.dispatch(sseSubagentSnapshot({ id: 'native:b', slot: 'chat-1', task: 'two', agent: 'kirocrew-helper', streaming: '', last_tool: '', started: 1020, engine: true, engine_turn: 'd4' }))
    render(<Provider store={store}><EngineSubagentLiveCard slot="chat-1" running /></Provider>)
    expect(screen.getByTestId('engine-batch-duration')).toHaveTextContent('1m 40s')
  })
})

describe('the batch card at a phone\'s width', () => {
  it('puts its duration and credits in a row of their own, not in the header with Panel and Show details', () => {
    render(<AssistantMessage {...BASE} engineSubagents={{ children: [child('one', 1, 2), { ...child('two', 1, 3), end: 'failed', reason: 'x' }], turn_credits: 0.5 }} onOpenSubagentPanel={() => {}} />)
    const row = screen.getByTestId('subagent-card-extra-chips')
    expect(row).toContainElement(screen.getByTestId('engine-batch-duration'))
    expect(row).toContainElement(screen.getByTestId('engine-batch-turn-credits'))
    expect(row).toHaveClass('flex-wrap')
    expect(row).not.toContainElement(screen.getByTestId('engine-subagent-record-toggle'))
  })
})
