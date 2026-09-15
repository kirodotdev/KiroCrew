/**
 * Evidence for the composer chip on a session that inherits its model.
 *
 * Both rows mount the REAL ChatInput and label the chip through the REAL
 * `displayModel()` on the same slot fixture — no pin, served list without
 * `auto` — so the only thing that differs is the argument this change adds.
 *
 *   BEFORE  displayModel(pin, models, degraded, withheld)              -> "auto"
 *   AFTER   displayModel(pin, models, degraded, withheld, served_model) -> the row
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
import { displayModel } from '../src/lib/model'
import { store } from '../src/store'
import { setActiveSlot } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

store.dispatch(setActiveSlot('capture-slot'))
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// A partition that does not serve `auto`: the backend's own default was not on
// this list, so the session was moved onto the first served model.
const SERVED = ['gpt-5.6-sol', 'gpt-5.6-terra', 'deepseek-3.2', 'glm-5', 'qwen3-coder-next'].map(name => ({ name }))
const PIN = ''
const WITHHELD = null
const SERVED_MODEL = 'gpt-5.6-sol'

const BEFORE = displayModel(PIN, SERVED, false, WITHHELD)
const AFTER = displayModel(PIN, SERVED, false, WITHHELD, SERVED_MODEL)

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

function Row({ episode, modelName }: { episode: string; modelName: string }) {
  const [value, setValue] = useState('')
  return (
    <div data-episode={episode} data-model-name={modelName} className="flex flex-col">
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => setValue('')}
        connected
        approvalMode="normal"
        modelName={modelName}
        // Same rule ChatPage/ChatPane apply: the served default is named
        // exactly when the pin alone would have read `auto`.
        modelIsInheritedDefault={modelName !== 'auto' && modelName !== BEFORE}
        onModelClick={() => {}}
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
      <Label>{`BEFORE — no pin, backend default not served: the chip can only say "${BEFORE}"`}</Label>
      <Row episode="before" modelName={BEFORE} />
      <Label>{`AFTER — same slot, session moved onto a served model: the chip names "${AFTER} · default"`}</Label>
      <Row episode="after" modelName={AFTER} />
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
