/**
 * Live-transcript recording fixture for the requeued-steer fix.
 *
 * Mounts the REAL ChatMessageList with its virtualized transcript (the same
 * VirtualTranscript ChatPane uses) and steps the message list through the
 * updates the gateway sends, in the order it sends them:
 *
 *   0. a steer is typed while the turn runs (row `steer: true`, pending)
 *   1. Stop lands before the turn took it: the row turns `requeued`, the Stop
 *      card is appended and the turn stops running
 *   2. the queue drain appends the queued turn's own row (chat_message)
 *   3. the drain marks the old row `superseded` (chat_message_update)
 *
 * The capture script drives `window.__setStep(n)` and records the result.
 * Query string: ?theme=dark|light
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { ThemeProvider } from '../src/hooks/useTheme'
import { initI18n } from '../src/i18n/all'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.dataset.theme = `kiro-${theme}`
document.documentElement.dataset.mode = theme

const STEER = 'Also check whether the search cluster is still on the old instance type.'

const history: ChatMessage[] = []
for (let i = 1; i <= 8; i++) {
  history.push({ role: 'user', content: `Earlier question ${i}: list the services in account ${i}.`, ts: `2026-10-09T14:${String(10 + 2 * i).padStart(2, '0')}:00Z`, meta: {} } as unknown as ChatMessage)
  history.push({
    role: 'assistant',
    content: `Account ${i} runs three services.\n\n- an API tier behind a load balancer\n- a queue worker fleet\n- a search cluster on r6g instances\n\nNothing there changed since last week.`,
    ts: `2026-10-09T14:${String(11 + 2 * i).padStart(2, '0')}:00Z`,
  } as unknown as ChatMessage)
}

const ask = { role: 'user', content: 'Audit account 9 the same way and flag anything unusual.', ts: '2026-10-09T14:50:00Z', meta: {} } as unknown as ChatMessage
const partial = { role: 'assistant', content: 'Looking at account 9 now. The API tier and the queue workers', ts: '2026-10-09T14:50:05Z' } as unknown as ChatMessage
const steerRow = (steerState?: string) =>
  ({ role: 'user', content: STEER, ts: '2026-10-09T14:52:10Z', meta: steerState ? { steer: true, steerState } : { steer: true } }) as unknown as ChatMessage
const stopData = { kind: 'stop_event', id: 'stop-1', state: 'stopped', outcome: 'soft', ts_start: '2026-10-09T14:52:20Z' }
const stopRow = { role: 'system', content: JSON.stringify(stopData), cls: JSON.stringify(stopData), meta: stopData } as unknown as ChatMessage
const freshRow = { role: 'user', content: STEER, ts: '2026-10-09T14:52:31Z', meta: {} } as unknown as ChatMessage

const STEPS: { messages: ChatMessage[]; running: boolean }[] = [
  { messages: [...history, ask, partial, steerRow()], running: true },
  { messages: [...history, ask, partial, steerRow('requeued'), stopRow], running: false },
  { messages: [...history, ask, partial, steerRow('requeued'), stopRow, freshRow], running: true },
  { messages: [...history, ask, partial, steerRow('superseded'), stopRow, freshRow], running: true },
]

declare global {
  interface Window { __setStep?: (n: number) => void; __step?: number }
}

function Scene() {
  const [step, setStep] = useState(0)
  useEffect(() => {
    window.__setStep = setStep
    window.__step = step
  }, [step])
  const s = STEPS[step]
  return (
    <div className="bg-bg text-text" data-capture-root style={{ width: 940, height: 820, display: 'flex', flexDirection: 'column' }}>
      <div className="px-4 py-2 text-text-muted" style={{ fontSize: 12 }} data-step-label>
        {['0. Steer typed while the turn runs', '1. Stop pressed before the turn took the steer', '2. Queue drain appends the queued turn\u2019s own row', '3. Drain marks the old row superseded'][step]}
      </div>
      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <ChatMessageList messages={s.messages} running={s.running} transcript={{ sessionId: 'capture:steer-requeue-live' }} />
      </div>
    </div>
  )
}

initI18n(params.get('lang') || 'en')
const qc = new QueryClient()
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <ThemeProvider>
      <MemoryRouter><Scene /></MemoryRouter>
    </ThemeProvider>
  </QueryClientProvider>,
)
