/**
 * Evidence sheet for the landed-steer highlight: a steer the turn ended before
 * reading is requeued, and the queue drain rebuilds its text as a new user row.
 * The rows come from the REAL chat reducer (`appendQueuedMessage` +
 * `removeQueuedMessage`, which stamps `meta.landedFromSteer`), and each is drawn
 * with the REAL UserMessage and StopEventCard.
 *
 * Nothing is drawn until the runner calls `window.__show()`, so the one-shot
 * flash starts under the runner's control. `window.__remount()` remounts every
 * row, the way the virtualized transcript does on a scroll away and back.
 *
 * Query string: ?theme=dark|light
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import StopEventCard from '../src/pages/chat/StopEventCard'
import UserMessage from '../src/pages/chat/UserMessage'
import reducer, { appendQueuedMessage, removeQueuedMessage } from '../src/store/chatSlice'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'capture-steer-landed'
const TEXT = 'Also check whether the search cluster is still on the old instance type.'

const render = (c: string) => <MarkdownRenderer content={c} softBreaks />

function stopRow(): ChatMessage {
  const data = { kind: 'stop_event', id: 'stop-1', state: 'stopped', outcome: 'soft', ts_start: '2026-11-05T14:52:20Z' }
  const json = JSON.stringify(data)
  return { role: 'system', content: json, cls: json, meta: data } as unknown as ChatMessage
}

/** The slot after the drain: the reducer's own output, not a hand-built row. */
function drainedRows(): ChatMessage[] {
  const seeded = structuredClone(reducer(undefined, { type: '@@INIT' }))
  seeded.slotMessages[SLOT] = [
    { role: 'user', content: TEXT, ts: '2026-11-05T14:52:10Z', cls: 'msg msg-u', meta: { mid: 'm-steer', steer: true, steerState: 'requeued' } },
    stopRow(),
  ]
  let state = reducer(seeded, appendQueuedMessage({ slot: SLOT, content: TEXT, ts: '2026-11-05T14:52:30Z', queue_id: 'q-1', meta: { requeuedSteer: true } }))
  state = reducer(state, removeQueuedMessage({ slot: SLOT, content: TEXT, queue_id: 'q-1' }))
  return state.slotMessages[SLOT] ?? []
}

function Row({ m }: { m: ChatMessage }) {
  if (m.role === 'system') {
    return <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 900 }}><StopEventCard message={m} /></div>
  }
  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 900 }} data-row-landed={m.meta?.landedFromSteer ? '' : undefined}>
      <div className="group flex flex-col min-w-0 items-end">
        <div className="flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full items-end">
          <UserMessage content={m.content} meta={m.meta} messageTs={m.ts} renderContent={render} />
        </div>
      </div>
    </div>
  )
}

declare global {
  interface Window { __show?: () => void; __remount?: () => void }
}

function Scene() {
  const [shown, setShown] = useState(false)
  const [generation, setGeneration] = useState(0)
  window.__show = () => setShown(true)
  window.__remount = () => setGeneration(g => g + 1)
  const rows = drainedRows()
  return (
    <div className="bg-bg text-text py-4" data-capture-root data-ready="" style={{ maxWidth: 940 }}>
      <div className="px-4 text-text-muted" style={{ fontSize: 12, margin: '10px 0 4px' }}>
        Stop pressed before the turn took the steer, then the queued turn starts: its row flashes once
      </div>
      {shown && rows.map((m, i) => <Row key={`${generation}-${i}`} m={m} />)}
    </div>
  )
}

initI18n(params.get('lang') || 'en')
createRoot(document.getElementById('root')!).render(<MemoryRouter><Scene /></MemoryRouter>)
