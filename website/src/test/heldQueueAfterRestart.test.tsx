import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import QueueStack, { splitPaneMessages } from '../components/QueueStack'
import { buildCommandCenter, type CommandCenterSources } from '../pages/chat/command-center/model'
import { hasLiveSessionWork, inferLane, readyQueueDepth } from '../pages/chat/sessionLane'
import { hydrateQueuedBubbles, normalizeQueueEntries, selectContinuable, selectTurnInterrupted } from '../store/chatSlice'
import type { ChatMessage, ChatSlot } from '../types'

/**
 * A queue the gateway restored after a restart is HELD: no drain will start it,
 * only Send now, an edit or a delete (`is_held_queue_entry` on the gateway).
 * Each reader below must not show it as work about to run: the sidebar row
 * reads interrupted, not "In progress"; the board files the chat under
 * Waiting, not Working; the card holds still; and Resume is offered, as the
 * transcript's restart notice says.
 */

const RESTART_NOTICE = 'This turn was interrupted when the app restarted. Review the partial output above, then resume to continue.'
const msg = (role: string, content = 'x', meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', ...(meta ? { meta } : {}) }) as ChatMessage
const interrupted = (): ChatMessage[] => [
  msg('user', 'first request'),
  msg('assistant', 'partial'),
  msg('error', RESTART_NOTICE, { kind: 'gateway_restart_interruption' }),
]
const chatState = (messages: ChatMessage[]) =>
  ({
    chat: { messages, slotRunning: false, slotStopping: false, pendingTurnSlot: null, activeSlot: 'slot-1' },
    dashboard: { slots: [] },
  }) as never

describe('the wire carries the held flag', () => {
  it('normalizes meta.held and nothing else onto the entry', () => {
    const [held, live] = normalizeQueueEntries([
      { id: 'q1', content: 'restored one', meta: { held: true } },
      { id: 'q2', content: 'live one', meta: { held: 'yes' } },
    ])
    expect(held.held).toBe(true)
    expect(live.held).toBeUndefined()
  })

  it('re-creates a crewmate row that was already held, so a Send now latch on it can retire', () => {
    const pinned = msg('queued', 'sent from the crewmate pane', { queueId: 'q1', responsiveSend: true, held: true })
    const rows = hydrateQueuedBubbles([msg('user', 'hello'), pinned], [
      { content: 'sent from the crewmate pane', queueId: 'q1', held: true },
    ])
    const kept = rows.find(m => m.meta?.queueId === 'q1')
    expect(kept?.meta?.held).toBe(true)
    expect(kept).not.toBe(pinned)
  })

  it('hydrates held queued rows and updates a crewmate row kept in place', () => {
    const pinned = msg('queued', 'sent from the crewmate pane', { queueId: 'q1', responsiveSend: true })
    const rows = hydrateQueuedBubbles([msg('user', 'hello'), pinned], [
      { queueId: 'q1', content: 'sent from the crewmate pane', ts: '', held: true },
      { queueId: 'q2', content: 'restored two', ts: '', held: true },
    ])
    const queued = rows.filter(m => m.role === 'queued')
    expect(queued.map(m => [m.meta?.queueId, m.meta?.held])).toEqual([['q1', true], ['q2', true]])
  })

  it('updates a live card a first pane hydrate keeps, so Resume is offered', () => {
    // A queue_push drew the card before the restart; the pane opens afterwards.
    const live = msg('queued', 'pushed before the restart', { queueId: 'q1' })
    const rows = hydrateQueuedBubbles([...interrupted(), live], [
      { queueId: 'q1', content: 'pushed before the restart', ts: '', held: true, rowWritten: true },
    ], true)
    const queued = rows.filter(m => m.role === 'queued')
    expect(queued.map(m => [m.meta?.queueId, m.meta?.held, m.meta?.rowWritten])).toEqual([['q1', true, true]])
    expect(selectContinuable(chatState(rows))).toBe(true)
  })
})

describe('Resume on an interrupted chat with held cards', () => {
  it('is offered past a held card, as the restart notice promises', () => {
    const s = chatState([...interrupted(), msg('queued', 'restored one', { queueId: 'q1', held: true })])
    expect(selectContinuable(s)).toBe(true)
    expect(selectTurnInterrupted(s)).toBe(true)
  })

  it('is withheld while the gateway counts a ready entry, before the rows catch up', () => {
    // The slots frame is ahead of this pane's rows: the gateway already counts
    // a ready entry (queue_held_depth below queue_depth) the rows still read held.
    const rows = [...interrupted(), msg('queued', 'restored one', { queueId: 'q1', held: true })]
    const s = {
      chat: { messages: rows, slotRunning: false, slotStopping: false, pendingTurnSlot: null, activeSlot: 'slot-1' },
      dashboard: { slots: [{ key: 'slot-1', queue_depth: 1, queue_held_depth: 0 }] },
    } as never
    expect(selectContinuable(s)).toBe(false)
    const stillHeld = {
      chat: { messages: rows, slotRunning: false, slotStopping: false, pendingTurnSlot: null, activeSlot: 'slot-1' },
      dashboard: { slots: [{ key: 'slot-1', queue_depth: 1, queue_held_depth: 1 }] },
    } as never
    expect(selectContinuable(stillHeld)).toBe(true)
  })

  it('is still withheld behind a card the gateway will run on its own', () => {
    const s = chatState([...interrupted(), msg('queued', 'sent after the restart', { queueId: 'q2' })])
    expect(selectContinuable(s)).toBe(false)
  })
})

describe('a held queue is not work in flight', () => {
  const restored = { interrupted: true, queue_depth: 2, queue_held_depth: 2 }

  it('counts only the cards a drain will start', () => {
    expect(readyQueueDepth(restored)).toBe(0)
    expect(readyQueueDepth({ queue_depth: 3, queue_held_depth: 1 })).toBe(2)
    // An older gateway sends no held count: every card is ready, as before.
    expect(readyQueueDepth({ queue_depth: 1 })).toBe(1)
  })

  it('leaves the interrupted chat waiting on the person instead of working', () => {
    expect(hasLiveSessionWork(restored)).toBe(false)
    expect(inferLane(restored)).toBe('waiting')
    expect(inferLane({ ...restored, queue_depth: 3 })).toBe('working')
  })

  it('shows the command center node idle', () => {
    const slot = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({ key, messages: 0, running: false, ...extra })
    const source: CommandCenterSources = {
      slots: [slot('root', restored)], root: 'root', subagents: {}, workflows: [], questions: [], approvals: [],
    }
    expect(buildCommandCenter(source).nodes[0].state).toBe('idle')
  })
})

describe('the held queue card', () => {
  it('says it is held and drops the waiting animation', () => {
    const { container } = render(
      <QueueStack messages={[msg('queued', 'restored one', { queueId: 'q1', held: true })]} onCancel={() => {}} onInterrupt={() => {}} />,
    )
    const label = screen.getByTestId('queue-card-held')
    expect(label).toHaveTextContent('Held')
    expect(label).toHaveAttribute('title', expect.stringContaining('Restored after a restart'))
    expect(container.querySelector('[class*="hourglass-flip"]')).toBeNull()
    // Send now stays: it is the one action that runs a held card.
    expect(screen.getByLabelText('Send now')).toBeInTheDocument()
  })

  it('shows a held prompt that reads like a system delivery as an actionable card', () => {
    // Restored entries are plain prompts by construction; their text decides nothing.
    const pasted = msg('queued', '[Subagent completion event] pasted by the person', { queueId: 'q3', held: true })
    const live = msg('queued', '[Subagent completion event] a real delivery', { queueId: 'q4' })
    const { queuedMessages, systemDeliveryCount } = splitPaneMessages([pasted, live])
    expect(queuedMessages).toEqual([pasted])
    expect(systemDeliveryCount).toBe(1)
    render(<QueueStack messages={queuedMessages} onCancel={() => {}} onInterrupt={() => {}} />)
    expect(screen.getByLabelText('Send now')).toBeInTheDocument()
  })

  it('offers only Send now on a held card whose row is already written', () => {
    render(
      <QueueStack messages={[msg('queued', 'already in the chat', { queueId: 'q5', held: true, rowWritten: true })]}
        onCancel={() => {}} onInterrupt={() => {}} onEdit={() => {}} />,
    )
    // The gateway refuses Cancel on it (409 `queue_item_already_sent`).
    expect(screen.getByLabelText('Send now')).toBeInTheDocument()
    expect(screen.queryByLabelText('Cancel queued message')).toBeNull()
    expect(screen.queryByLabelText('Edit queued message')).toBeNull()
    // Its hint offers only what the card has left.
    expect(screen.getByTestId('queue-card-held').getAttribute('title')).toBe(
      "Restored after a restart. It's already in the conversation, so you can only send it now.",
    )
  })

  it("disables a held card's Send now while a turn runs", () => {
    const held = msg('queued', 'restored one', { queueId: 'q1', held: true })
    const { rerender } = render(<QueueStack messages={[held]} onInterrupt={() => {}} turnRunning />)
    // The gateway refuses it behind a running turn (409 `held_turn_running`).
    expect(screen.getByLabelText('Send now')).toBeDisabled()
    expect(screen.getByLabelText('Send now').getAttribute('title')).toMatch(/turn ends/i)
    rerender(<QueueStack messages={[held]} onInterrupt={() => {}} turnRunning={false} />)
    expect(screen.getByLabelText('Send now')).toBeEnabled()
  })

  it('keeps Send now enabled on a ready card while a turn runs', () => {
    render(<QueueStack messages={[msg('queued', 'live one', { queueId: 'q2' })]} onInterrupt={() => {}} turnRunning />)
    expect(screen.getByLabelText('Send now')).toBeEnabled()
  })

  it('keeps the hourglass for a card that is next to run', () => {
    const { container } = render(<QueueStack messages={[msg('queued', 'live one', { queueId: 'q2' })]} onCancel={() => {}} />)
    expect(screen.queryByTestId('queue-card-held')).toBeNull()
    expect(container.querySelector('[class*="hourglass-flip"]')).not.toBeNull()
  })
})
