/**
 * Evidence sheet for the requeued-steer duplicate fix: the same transcript rows
 * at three moments, drawn with the REAL UserMessage and StopEventCard, and
 * filtered through the REAL isSupersededSteerRow predicate the transcript uses.
 *
 *   1. stopped: the steer row is marked `requeued` above the Stop card.
 *   2. drained on main: the queued turn appended a second row with the same text.
 *   3. drained with the fix: the old row is `superseded` and not drawn.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import StopEventCard from '../src/pages/chat/StopEventCard'
import UserMessage from '../src/pages/chat/UserMessage'
import { isSupersededSteerRow } from '../src/pages/chat/groupDisplayItems'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const TEXT = 'Also check whether the search cluster is still on the old instance type.'

const render = (c: string) => <MarkdownRenderer content={c} softBreaks />

function stopRow(): ChatMessage {
  const data = { kind: 'stop_event', id: 'stop-1', state: 'stopped', outcome: 'soft', ts_start: '2026-10-09T14:52:20Z' }
  const json = JSON.stringify(data)
  return { role: 'system', content: json, cls: json, meta: data } as unknown as ChatMessage
}

function steerRow(steerState: string): ChatMessage {
  return { role: 'user', content: TEXT, ts: 'steer-row', meta: { steer: true, steerState } } as unknown as ChatMessage
}

function freshRow(): ChatMessage {
  return { role: 'user', content: TEXT, ts: 'fresh-row', meta: {} } as unknown as ChatMessage
}

const EPISODES: { id: string; label: string; rows: ChatMessage[] }[] = [
  { id: 'stopped', label: '1. Stop pressed before the turn took the steer', rows: [steerRow('requeued'), stopRow()] },
  { id: 'main', label: '2. Queued turn starts, main today: the text is drawn twice', rows: [steerRow('requeued'), stopRow(), freshRow()] },
  { id: 'fix', label: '3. Queued turn starts, this PR: the old row is superseded and not drawn', rows: [steerRow('superseded'), stopRow(), freshRow()] },
]

function Row({ m }: { m: ChatMessage }) {
  if (m.role === 'system') {
    return <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 900 }}><StopEventCard message={m} /></div>
  }
  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 900 }}>
      <div className="group flex flex-col min-w-0 items-end">
        <div className="flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full items-end">
          <UserMessage content={m.content} meta={m.meta} messageTs={(m as { ts?: string }).ts} renderContent={render} />
        </div>
      </div>
    </div>
  )
}

function Scene() {
  return (
    <div className="bg-bg text-text py-4" data-capture-root style={{ maxWidth: 940 }}>
      {EPISODES.map(ep => (
        <div key={ep.id} data-episode={ep.id} style={{ marginBottom: 18 }}>
          <div className="px-4 text-text-muted" style={{ fontSize: 12, margin: '10px 0 4px' }}>{ep.label}</div>
          {ep.rows.filter(m => !isSupersededSteerRow(m)).map((m, i) => <Row key={i} m={m} />)}
        </div>
      ))}
    </div>
  )
}

initI18n(params.get('lang') || 'en')
createRoot(document.getElementById('root')!).render(<MemoryRouter><Scene /></MemoryRouter>)
