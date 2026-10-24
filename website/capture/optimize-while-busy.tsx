/**
 * Evidence for #23572: the composer keeps "Optimize prompt" while a turn runs.
 *
 * Every row mounts the REAL ChatInput with the props a chat host passes. Only
 * the turn state and the draft differ between rows:
 *
 *   IDLE        no turn, a draft            -> Optimize button + Send
 *   QUEUE       a turn runs, a draft        -> Queue message; Optimize in the "+" menu
 *   STEER       a turn runs, a draft, steer -> the steer/queue split; Optimize in "+"
 *   STOP        a turn runs, no draft       -> Stop; Optimize in "+" (disabled)
 *
 *   ?theme=dark|light
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatInput from '../src/components/ChatInput'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { setActiveSlot } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

store.dispatch(setActiveSlot('capture-slot'))
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const DRAFT = 'also check the staging logs once the deploy finishes'

type Episode = {
  id: string
  running: boolean
  draft: string
  steer: boolean
  caption: string
}

const EPISODES: Episode[] = [
  { id: 'idle', running: false, draft: DRAFT, steer: false, caption: 'IDLE — no turn running, a draft typed' },
  { id: 'queue', running: true, draft: DRAFT, steer: false, caption: 'TURN RUNNING — a draft typed, sends queue' },
  { id: 'steer', running: true, draft: DRAFT, steer: true, caption: 'TURN RUNNING — a draft typed, steer or queue' },
  { id: 'stop', running: true, draft: '', steer: false, caption: 'TURN RUNNING — empty composer' },
]

function Label({ children }: { children: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        opacity: 0.55,
        margin: '18px 0 6px',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      }}
    >
      {children}
    </div>
  )
}

function Row({ episode }: { episode: Episode }) {
  const [value, setValue] = useState(episode.draft)
  return (
    <div data-episode={episode.id} className="flex flex-col">
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => {}}
        connected
        approvalMode="normal"
        isRunning={episode.running}
        onStop={() => {}}
        canSteer={episode.steer}
        onSteer={episode.steer ? () => {} : undefined}
        onUploadFiles={() => {}}
      />
    </div>
  )
}

function Scene() {
  return (
    <div
      data-capture-root
      className="bg-bg text-text"
      style={{ maxWidth: 760, margin: '0 auto', padding: '20px 24px 28px' }}
    >
      {EPISODES.map(episode => (
        <div key={episode.id}>
          <Label>{episode.caption}</Label>
          <Row episode={episode} />
        </div>
      ))}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Scene />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
