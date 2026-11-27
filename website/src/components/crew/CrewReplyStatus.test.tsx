/**
 * The worker status line under a conductor's reply: drawn only with its Feature
 * Preview on, only under the newest finished reply, only for a conductor root
 * that is not a Bot's chat, and only while some worker needs the person, runs
 * or waits. A click opens Workers for that conductor.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from '../../test/helpers'
import chatReducer from '../../store/chatSlice'
import dashboardReducer from '../../store/dashboardSlice'
import type { ChatMessage, ChatSlot } from '../../types'
import { PREVIEW_CREW_REPLY_STATUS } from '../../utils/previewFlags'

const list = vi.hoisted(() => ({ workers: [] as unknown[], calls: 0, fail: false }))
vi.mock('../../api/client', () => ({
  api: { crewWorkers: () => { list.calls++; return list.fail ? Promise.reject(new Error('down')) : Promise.resolve({ root: 'root', workers: list.workers }) } },
}))

import { CrewReplyStatus } from './CrewReplyStatus'
import { latestFooterReply } from '../../pages/chat/replyFooter'
import { takeCrewPanelView } from '../../pages/chat/command-center/crewPanelView'

const worker = (key: string, group: string, status = 'idle') =>
  ({ key, title: key, agent: '', status, group, last_activity: '', ledger_item: '', created_by: 'root' })

const older: ChatMessage = { role: 'assistant', content: 'First plan', ts: '2026-11-27T01:00:00Z' }
const newest: ChatMessage = { role: 'assistant', content: 'Dispatched two workers', ts: '2026-11-27T01:05:00Z' }
const TRANSCRIPT: ChatMessage[] = [
  { role: 'user', content: 'Plan it', ts: '2026-11-27T00:59:00Z' },
  older,
  { role: 'user', content: 'Go', ts: '2026-11-27T01:04:00Z' },
  newest,
]

function renderReplies(opts: { slot?: Partial<ChatSlot>; messages?: ChatMessage[]; rows?: ChatMessage[]; onOpen?: (slot: string) => void; activeSlot?: string; pane?: ChatMessage[] } = {}) {
  const messages = opts.messages ?? TRANSCRIPT
  const store = createTestStore({
    chat: { ...chatReducer(undefined, { type: '@@test/init' }), activeSlot: opts.activeSlot ?? 'root', messages, slotRunning: false },
    dashboard: { ...dashboardReducer(undefined, { type: '@@test/init' }), slots: [{ key: 'root', title: 'Conductor', crew_mode_root: true, ...opts.slot } as ChatSlot] },
  } as never)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onOpen = opts.onOpen ?? (() => {})
  render(
    <MemoryRouter><QueryClientProvider client={qc}><Provider store={store}>
      {(opts.rows ?? [older, newest]).map(m => (
        <div key={m.ts} data-testid={`reply-${m.ts}`}>
          <CrewReplyStatus slotKey="root" message={m} onOpenWorkers={onOpen} messages={opts.pane} running={opts.pane ? false : undefined} />
        </div>
      ))}
    </Provider></QueryClientProvider></MemoryRouter>,
  )
  return { qc }
}

describe('CrewReplyStatus', () => {
  beforeEach(() => {
    localStorage.setItem(PREVIEW_CREW_REPLY_STATUS, '1')
    list.workers = [worker('w1', 'working', 'running'), worker('w2', 'approval', 'needs_approval'), worker('w3', 'waiting')]
    list.calls = 0
    list.fail = false
  })
  afterEach(() => { localStorage.clear() })

  it('draws nothing and reads no worker list with the preview off', async () => {
    localStorage.removeItem(PREVIEW_CREW_REPLY_STATUS)
    const { qc } = renderReplies()
    // The gates are synchronous: off, no row reads the worker list at all.
    expect(qc.getQueryState(['crew-workers', 'root'])).toBeUndefined()
    expect(list.calls).toBe(0)
    expect(screen.queryByTestId('crew-reply-status')).toBeNull()
  })

  it('draws only under the newest reply, never an older one', async () => {
    renderReplies()
    const line = await screen.findByTestId('crew-reply-status')
    expect(screen.getAllByTestId('crew-reply-status')).toHaveLength(1)
    expect(screen.getByTestId(`reply-${newest.ts}`)).toContainElement(line)
    expect(line).toHaveTextContent('1 needs you')
    expect(line).toHaveTextContent('1 running')
    expect(line).toHaveTextContent('1 waiting')
  })

  it('draws in a split-view pane from the pane\'s own rows, under its newest reply', async () => {
    // Another chat is the active one: the store's rows are not this pane's.
    renderReplies({ activeSlot: 'other', messages: [], pane: TRANSCRIPT })
    const line = await screen.findByTestId('crew-reply-status')
    expect(screen.getAllByTestId('crew-reply-status')).toHaveLength(1)
    expect(screen.getByTestId(`reply-${newest.ts}`)).toContainElement(line)
  })

  it('counts a waiting worker whose ledger item stalled once, as stalled', async () => {
    renderReplies({ slot: { crew_work: { open: 1, person: 0, conductor_looping: false, items: [{ item_id: 'it-1', worker: 'w3', stale_at_ms: 1 }] } as never } })
    const line = await screen.findByTestId('crew-reply-status')
    await waitFor(() => expect(line).toHaveTextContent('1 stalled'))
    // w3 is the one waiting worker: shown as stalled, it is not also "waiting".
    expect(line).not.toHaveTextContent('waiting')
    expect(line).toHaveTextContent('1 running')
  })

  it('goes away when no worker needs you, runs or waits', async () => {
    list.workers = [worker('w1', 'done')]
    const { qc } = renderReplies()
    await waitFor(() => expect(qc.getQueryState(['crew-workers', 'root'])?.status).toBe('success'))
    expect(screen.queryByTestId('crew-reply-status')).toBeNull()
    expect(screen.queryByTestId('crew-root-done')).toBeNull()
  })

  it('says a failed worker read in the line\'s place', async () => {
    list.fail = true
    renderReplies()
    expect(await screen.findByTestId('crew-reply-status-error')).toHaveTextContent(/.+/)
    expect(screen.queryByTestId('crew-reply-status')).toBeNull()
  })

  it('opens Workers for this conductor on a click', async () => {
    const onOpen = vi.fn()
    renderReplies({ onOpen })
    const line = await screen.findByTestId('crew-reply-status')
    expect(line.tagName).toBe('BUTTON')
    // It says in words that it opens Workers, beside the counts.
    expect(screen.getByTestId('crew-reply-status-open')).toHaveTextContent('Workers')
    expect(line.getAttribute('aria-label')).toMatch(/^Workers: .*1 needs you.*\. Open the Workers panel$/)
    fireEvent.click(line)
    expect(onOpen).toHaveBeenCalledWith('root')
    // The Board panel is asked to show its Workers list, not Status.
    expect(takeCrewPanelView('root')).toBe('list')
  })

  it("draws nothing in a Bot's chat", async () => {
    const { qc } = renderReplies({ slot: { agent_kind: 'member' } })
    expect(qc.getQueryState(['crew-workers', 'root'])).toBeUndefined()
    expect(list.calls).toBe(0)
    expect(screen.queryByTestId('crew-reply-status')).toBeNull()
  })

  it('draws nothing in a chat that is not a conductor', async () => {
    const { qc } = renderReplies({ slot: { crew_mode_root: false } })
    expect(qc.getQueryState(['crew-workers', 'root'])).toBeUndefined()
    expect(list.calls).toBe(0)
    expect(screen.queryByTestId('crew-reply-status')).toBeNull()
  })
})

describe('latestFooterReply', () => {
  it('is the newest finished reply', () => {
    expect(latestFooterReply(TRANSCRIPT, false)).toBe(newest)
  })

  it('is none while the newest reply is still streaming, not an older one', () => {
    const streaming: ChatMessage = { role: 'streaming', content: 'Working', ts: '2026-11-27T01:06:00Z' }
    expect(latestFooterReply([...TRANSCRIPT, { role: 'user', content: 'More', ts: '2026-11-27T01:05:30Z' }, streaming], true)).toBeNull()
  })

  it('passes over a system notice after the reply, such as a compaction', () => {
    const notice: ChatMessage = { role: 'assistant', kind: 'compaction', content: 'Context summary', ts: '2026-11-27T01:06:00Z' } as ChatMessage
    expect(latestFooterReply([...TRANSCRIPT, notice], false)).toBe(newest)
  })

  it('is none while the newest reply\'s turn is still running', () => {
    expect(latestFooterReply(TRANSCRIPT, true)).toBeNull()
  })
})
