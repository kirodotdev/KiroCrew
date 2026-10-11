/**
 * Evidence for #7263: the composer's project chip while a response is running.
 *
 * Mounts the REAL ChatInput with `isRunning`, so the project chip renders with
 * whatever enabled/disabled state and tooltip the shipped component gives it.
 * The agent and model chips are mounted beside it to show they stay locked.
 * Headless Chromium cannot paint a native `title`, so the chip's accessible
 * name (which mirrors the title) is printed under the composer.
 *
 *   ?theme=dark|light
 */
import { useEffect, useState } from 'react'
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

function Scene() {
  const [value, setValue] = useState('')
  const [label, setLabel] = useState('')
  const [clicks, setClicks] = useState(0)
  useEffect(() => {
    const chip = document.querySelector('[data-capture-root] button[aria-label^="Project: "]')
    setLabel(chip?.getAttribute('aria-label') || '(chip not found)')
  }, [])
  return (
    <div data-capture-root className="bg-bg text-text" style={{ maxWidth: 760, margin: '0 auto', padding: '20px 24px 28px' }}>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => setValue('')}
        connected
        approvalMode="normal"
        isRunning
        onStop={() => {}}
        project="/home/user/work/KiroCrew"
        projectBranch="main"
        onProjectClick={() => setClicks(c => c + 1)}
        agentName="kirocrew"
        onAgentClick={() => {}}
        modelName="claude-sonnet-4"
        onModelClick={() => {}}
      />
      <pre data-capture-label style={{ marginTop: 14, fontSize: 12, opacity: 0.75, whiteSpace: 'pre-wrap' }}>
        {`Project chip tooltip / accessible name while a response runs:\n${label}\nPicker opened: ${clicks}`}
      </pre>
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
