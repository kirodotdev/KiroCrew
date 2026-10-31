/**
 * Evidence for the reasoning-effort block in the model picker on Claude Haiku.
 *
 * Mounts the REAL ModelEffortDropdown twice, side by side, on the two Haiku ids
 * kiro-cli serves. Each picker's `hasEffort` is the REAL `modelSupportsEffort()`
 * for its active model, the same fallback the composer uses when the slot has
 * no capability verdict yet, so the only input is the model id:
 *
 *   left   claude-haiku-5.5  (kiro-cli accepts /effort)
 *   right  claude-haiku-4.5  (kiro-cli refuses /effort)
 *
 *   ?theme=dark|light
 */
import { useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ModelEffortDropdown from '../src/components/ModelEffortDropdown'
import { initI18n } from '../src/i18n/all'
import { modelSupportsEffort } from '../src/lib/effort'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const MODELS = ['claude-haiku-5.5', 'claude-haiku-4.5', 'claude-sonnet-5.5', 'claude-opus-5.5'].map(name => ({
  name,
  display_name: name,
}))
const ANCHOR_TOP = 600
const COLUMN = 380

function Picker({ episode, model, column }: { episode: string; model: string; column: number }) {
  const [filter, setFilter] = useState('')
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const right = (column + 1) * COLUMN - 20
  const anchorRect = new DOMRect(right - 120, ANCHOR_TOP, 120, 24)
  const hasEffort = modelSupportsEffort(model)
  return (
    <div data-episode={episode} data-has-effort={String(hasEffort)}>
      <div
        style={{
          position: 'fixed',
          left: column * COLUMN + 20,
          top: ANCHOR_TOP + 8,
          fontSize: 12,
          fontFamily: 'ui-sans-serif, system-ui, sans-serif',
        }}
        className="text-muted"
      >
        {`${model}: modelSupportsEffort() = ${hasEffort}`}
      </div>
      <ModelEffortDropdown
        anchorRect={anchorRect}
        dropdownRef={dropdownRef}
        inputRef={inputRef}
        models={MODELS}
        activeModel={model}
        onSelectModel={() => {}}
        filter={filter}
        setFilter={setFilter}
        onClose={() => {}}
        hasEffort={hasEffort}
        slot="capture-slot"
        currentEffort="high"
        onListKeyDown={() => {}}
      />
    </div>
  )
}

function Scene() {
  return (
    <div data-capture-root className="bg-bg text-text" style={{ width: COLUMN * 2, height: ANCHOR_TOP + 40 }}>
      <Picker episode="haiku-5.5" model="claude-haiku-5.5" column={0} />
      <Picker episode="haiku-4.5" model="claude-haiku-4.5" column={1} />
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
