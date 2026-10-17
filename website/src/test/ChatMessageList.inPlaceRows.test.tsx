/**
 * Rows that change in place keep changing while ChatMessageList reuses its
 * settled rows. The list's virtualized mount (the `transcript` prop, what
 * ChatPane, SideChat and ChatEmbed use) hands a settled row the same props
 * across a streaming flush, so a row redraws only when its own messages, the
 * host's props or the rest of the transcript change. Each case below changes
 * what one settled row draws from, streams a chunk the way a live reply does,
 * and reads the row. Every row component here is the real one: AssistantMessage,
 * UserMessage, ThreadFooter, the tool pill, the change card, the markdown.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { store as reduxStore } from '../store'
import { applyCardUpdate, type Card } from '../api/cards'
import { changeLanguage } from '../i18n/all'
import { fmtRelative } from '../i18n/format'
import { i18nT } from '../i18n/t'
import ChatMessageList, { type ChatMessageListProps } from '../app-sdk/ChatMessageList'
import type { ThreadHooks } from '../app-sdk/messageRenderers'
import { fmtMessageTime, fmtMessageTimeFull } from '../pages/chat/messageTime'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import type { ChatMessage } from '../types'

const at = (s: number) => `2026-03-02T10:00:${String(s).padStart(2, '0')}Z`
const user = (content: string, s: number, meta?: Record<string, unknown>): ChatMessage =>
  ({ role: 'user', content, cls: '', ts: at(s), ...(meta ? { meta } : {}) })
const reply = (content: string, s: number, meta?: Record<string, unknown>): ChatMessage =>
  ({ role: 'assistant', content, cls: '', ts: at(s), ...(meta ? { meta } : {}) })
const streaming = (content: string): ChatMessage => ({ role: 'streaming', content, cls: '' })

// Each mount gets its own height-cache partition, so no case restores another's.
let mountSeq = 0
let sessionId = ''
const list = (messages: ChatMessage[], props: Partial<ChatMessageListProps> = {}) => (
  <ChatMessageList messages={messages} running={false} transcript={{ sessionId }} {...props} />
)

/** The measured row block (`data-display-index`) that holds this text. */
function rowOf(text: string): HTMLElement {
  const row = screen.getByText(text).closest('[data-display-index]')
  if (!row) throw new Error(`no transcript row holds ${JSON.stringify(text)}`)
  return row as HTMLElement
}

/** A reply's footer, found by its full timestamp (the footer's `title`). */
const footerOf = (m: ChatMessage) => within(rowOf(m.content)).queryByTitle(fmtMessageTimeFull(m.ts))

beforeEach(() => {
  localStorage.clear()
  sessionId = `test:in-place-${++mountSeq}`
})

describe('a settled reply footer', () => {
  it('appears when the turn streaming below it ends in an error', () => {
    const q1 = user('First question?', 1)
    const a1 = reply('First answer.', 2)
    const view = render(list([q1, a1, streaming('Work')], { running: true }))
    // A reply is still being written below a1, so the turn goes on: no footer.
    expect(footerOf(a1)).toBeNull()
    view.rerender(list([q1, a1, streaming('Working on it')], { running: true }))
    expect(footerOf(a1)).toBeNull()
    view.rerender(list([q1, a1, { role: 'error', content: 'The model stopped.', cls: '', ts: at(3) }]))
    expect(screen.getByText('The model stopped.')).toBeInTheDocument()
    expect(footerOf(a1)).not.toBeNull()
  })

  it('moves to the row below when a quiet row there is rewritten into words', () => {
    const q1 = user('First question?', 1)
    const a1 = reply('First answer.', 2)
    // A say-nothing reply (a bare zero-width space) draws nothing, so a1 ends the turn.
    const view = render(list([q1, a1, reply('\u200b', 3)]))
    expect(footerOf(a1)).not.toBeNull()
    const second = reply('Second thought.', 3)
    view.rerender(list([q1, a1, second]))
    expect(footerOf(a1)).toBeNull()
    expect(footerOf(second)).not.toBeNull()
  })
})

describe('a replaced message', () => {
  it('draws its new content in place while the reply below it streams', () => {
    const view = render(list([user('Old wording', 1), streaming('Re')], { running: true }))
    view.rerender(list([user('New wording', 1), streaming('Reading')], { running: true }))
    expect(screen.getByText('New wording')).toBeInTheDocument()
    expect(screen.queryByText('Old wording')).toBeNull()
  })
})

describe('the steer chips', () => {
  it("draw the steer's badge when the server confirms it and the reply's chip when the ack streams in", () => {
    const q1 = user('Fix the build.', 1)
    const steerText = 'Also run the linter.'
    const view = render(list([q1, streaming('Building'), user(steerText, 2, { steer: true, optimistic: true })], { running: true }))
    // An unconfirmed steer is not drawn as steered.
    expect(screen.queryByText('Steered into the running turn')).toBeNull()
    // The server's echo settles the steer bubble in place while the reply streams on.
    const confirmed = user(steerText, 2, { steer: true, steerState: 'consumed' })
    view.rerender(list([q1, streaming('Building the project'), confirmed], { running: true }))
    expect(screen.getByText('Steered into the running turn')).toBeInTheDocument()
    // kiro-cli acknowledges the steer inside the reply: the chip draws as the
    // marker streams in, and stays when the reply settles.
    const acked = 'Building the project. [STEERING steer-1: running the linter too] Both pass.'
    view.rerender(list([q1, streaming(acked), confirmed], { running: true }))
    expect(screen.getByText('Steered')).toBeInTheDocument()
    view.rerender(list([q1, reply(acked, 3), confirmed]))
    expect(screen.getByText('Steered')).toBeInTheDocument()
    expect(screen.getByText('Steered into the running turn')).toBeInTheDocument()
  })
})

describe('a tool row', () => {
  /** The pill's button, which carries the status tone. */
  const pillOf = (label: RegExp) => {
    const button = screen.getByText(label).closest('button')
    if (!button) throw new Error(`no tool pill reads ${label}`)
    return button
  }

  it('follows its result, and a policy block on it, while the reply below streams', () => {
    const q1 = user('Run the tests.', 1)
    const tests = { role: 'tool', content: '🔧 npm test', cls: '', ts: at(2), meta: { tool_call_id: 'tc-1' } }
    const lint = { role: 'tool', content: '🔧 npm run lint', cls: '', ts: at(3), meta: { tool_call_id: 'tc-2' } }
    const a1 = reply('Running them now.', 4)
    const q2 = user('Anything else?', 5)
    const view = render(list([q1, tests, lint, a1, q2, streaming('Che')], { running: true }))
    // A finished turn folds its steps behind one toggle (the row after the
    // question); open it, and it stays open across the redraws below.
    const fold = view.container.querySelector('[data-display-index="1"] button[aria-expanded="false"]')
    if (!fold) throw new Error('the finished turn has no fold toggle')
    fireEvent.click(fold)
    expect(pillOf(/npm test/)).toHaveClass('text-accent')
    // The result lands on the 🔧 row itself.
    const done = { ...tests, meta: { ...tests.meta, output: '12 passed' } }
    view.rerender(list([q1, done, lint, a1, q2, streaming('Checking')], { running: true }))
    expect(pillOf(/npm test/)).toHaveClass('text-ok-text')
    // A policy blocks the second call: its hidden 🚫 sibling turns that pill amber.
    const denied = { role: 'tool', content: '🚫 Blocked by policy', cls: '', ts: at(3), meta: { tool_call_id: 'tc-2' } }
    view.rerender(list([q1, done, lint, denied, a1, q2, streaming('Checking the')], { running: true }))
    expect(pillOf(/npm run lint/)).toHaveClass('text-warn-text')
    view.rerender(list([q1, done, lint, denied, a1, q2, streaming('Checking the logs')], { running: true }))
    expect(pillOf(/npm test/)).toHaveClass('text-ok-text')
    expect(pillOf(/npm run lint/)).toHaveClass('text-warn-text')
  })
})

describe('a thread footer', () => {
  afterEach(() => { vi.useRealTimers() })

  it('keeps its last-reply age current with no row changing, and its count with the thread', () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(Date.parse(at(30)))
    const q1 = user('First question?', 1)
    const a1 = reply('First answer.', 2, { mid: 'm-a1' })
    const q2 = user('Second question?', 3)
    const threadsWith = (count: number, lastReply: string): ThreadHooks => ({
      summaryOf: (mid) => (mid === 'm-a1' ? { count, last_reply_ts: lastReply, participants: ['user', 'assistant'] } : undefined),
      onOpen: () => {},
      crewmateName: 'Radar',
    })
    const threads = threadsWith(2, at(5))
    const view = render(list([q1, a1, q2, streaming('Thin')], { running: true, threads }))
    const footer = () => screen.getByTestId('thread-footer')
    expect(footer()).toHaveTextContent('2 replies')
    expect(footer()).toHaveTextContent(fmtRelative(at(5)))
    // Three minutes on, with nothing in the transcript changing but the stream.
    act(() => { vi.advanceTimersByTime(180_000) })
    view.rerender(list([q1, a1, q2, streaming('Thinking')], { running: true, threads }))
    const threeMinutesOn = fmtRelative(at(5))
    expect(threeMinutesOn).not.toBe(fmtRelative(at(5), { now: Date.parse(at(30)) }))
    expect(footer()).toHaveTextContent(threeMinutesOn)
    // A new reply in the thread: the host hands a new summary.
    view.rerender(list([q1, a1, q2, streaming('Thinking it')], { running: true, threads: threadsWith(3, new Date().toISOString()) }))
    expect(footer()).toHaveTextContent('3 replies')
  })
})

describe('a language or theme switch', () => {
  beforeEach(() => {
    // The footer date drops the year only for this year's messages.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(Date.parse(at(30)))
  })
  afterEach(async () => {
    vi.useRealTimers()
    await act(async () => { await changeLanguage('en') })
    delete document.documentElement.dataset.theme
    delete document.documentElement.dataset.mode
  })

  it('redraws every settled row in the new language, with no row changing', async () => {
    const q1 = user('First question?', 1)
    const a1 = reply('First answer.', 2)
    const q2 = user('Second question?', 3)
    render(list([q1, a1, q2, streaming('Thinking')], { running: true }))
    const english = fmtMessageTime(a1.ts)
    expect(rowOf('First answer.')).toHaveTextContent(english)
    await act(async () => { await changeLanguage('zh-CN') })
    // The footer's date is formatted by the list, not by the bubble, so only a
    // redraw of the row itself can change it.
    const chinese = fmtMessageTime(a1.ts)
    expect(chinese).not.toBe(english)
    expect(rowOf('First answer.')).toHaveTextContent(chinese)
    // The bubble's own strings switch too: its Copy button reads the new catalog.
    const copy = i18nT('pages.chat.assistantMessage.copy')
    expect(copy).not.toBe('Copy')
    expect(within(rowOf('First answer.')).getByRole('button', { name: copy })).toBeInTheDocument()
  })

  it('needs no redraw for a theme: a reused row reads like a fresh one under the new theme', () => {
    const messages = (tail: string) => [user('First question?', 1), reply('First answer.', 2), user('Second question?', 3), streaming(tail)]
    // React's generated ids differ from one mount to the next; nothing else may.
    const markup = (el: HTMLElement) => el.outerHTML.replace(/ (id|aria-controls|aria-describedby|aria-labelledby)="[^"]*"/g, '')
    document.documentElement.dataset.theme = 'light'
    document.documentElement.dataset.mode = 'light'
    const view = render(list(messages('Thin'), { running: true }))
    document.documentElement.dataset.theme = 'dark'
    document.documentElement.dataset.mode = 'dark'
    view.rerender(list(messages('Thinking'), { running: true }))
    const reused = markup(rowOf('First answer.'))
    view.unmount()
    sessionId = `test:in-place-${++mountSeq}`
    render(list(messages('Thinking'), { running: true }))
    expect(markup(rowOf('First answer.'))).toBe(reused)
  })
})

describe('earlier history loaded above', () => {
  const q2 = user('Second question?', 5)
  const a2 = reply('Second answer.', 6)
  const q3 = user('Third question?', 7)
  // A sub-agent row the card cannot parse is never drawn, so it moves every
  // transcript index below it without adding a display row.
  const undrawn = { role: 'subagent', content: 'internal bookkeeping', cls: '', ts: at(1) }

  it('re-indexes the rows below it', () => {
    const view = render(list([q2, a2, q3, streaming('Thin')], { running: true }))
    expect(footerOf(a2)).not.toBeNull()
    const q1 = user('First question?', 2)
    const a1 = reply('First answer.', 3)
    view.rerender(list([undrawn, q1, a1, q2, a2, q3, streaming('Thinking')], { running: true }))
    expect(footerOf(a1)).not.toBeNull()
    expect(footerOf(a2)).not.toBeNull()
  })

  it('re-indexes them when it holds only a row that is never drawn', () => {
    const view = render(list([q2, a2, q3, streaming('Thin')], { running: true }))
    view.rerender(list([undrawn, q2, a2, q3, streaming('Thinking')], { running: true }))
    // Read against a stale index, a2 would see itself as the row that follows it.
    expect(footerOf(a2)).not.toBeNull()
  })
})

describe('a change card', () => {
  const SLOT = 'slot-in-place'
  const NOW = Date.UTC(2026, 2, 2, 10)
  const card: Card = {
    id: 'c1',
    slot_key: SLOT,
    kind: 'setting.change',
    revision: 1,
    status: 'pending',
    risk: 'normal',
    title: 'Shorter replies',
    changes: [{ label: 'Reply length', before: 'standard', after: 'brief' }],
    editable: [],
    params: { path: 'chat.verbosity', value: 'brief' },
    plan: {
      apply: [{ method: 'PATCH', path: '/api/config/kirocrew', body: { path: 'chat.verbosity', value: 'brief' } }],
      undo: [{ method: 'PATCH', path: '/api/config/kirocrew', body: { path: 'chat.verbosity', value: 'standard' } }],
    },
    created_at: 1_700_000_000,
    expires_at: NOW + 3_600_000,
  }
  const cardRow: ChatMessage = {
    role: 'card',
    content: card.title,
    cls: 'msg msg-card',
    ts: at(2),
    meta: { mid: 'm-c1', card: { surface: 'change', id: card.id, slot: SLOT, kind: card.kind, title: card.title, status: card.status } },
  }

  afterEach(() => { vi.useRealTimers() })

  it('follows the card store while the reply below it streams', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    server.use(http.get('/api/cards/pending', () => HttpResponse.json({ cards: [card] })))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const renderers = createTranscriptRenderers({ slot: SLOT })
    const mount = (tail: string) => (
      <Provider store={reduxStore}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            {list([user('Make replies shorter.', 1), cardRow, reply('Proposed it above.', 3), user('Thanks.', 4), streaming(tail)], { running: true, renderers })}
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>
    )
    const view = render(mount('You'))
    expect(await screen.findByTestId('change-card-apply')).toBeInTheDocument()
    // The gateway's card_update frame folds into the card store; the row itself does not change.
    act(() => applyCardUpdate(qc, { ...card, status: 'applied' }))
    view.rerender(mount("You're welcome"))
    // The pending card collapses into its result line.
    expect(await screen.findByTestId('change-card-result')).toHaveTextContent('Reply length')
    expect(screen.queryByTestId('change-card-apply')).toBeNull()
  })
})
