/**
 * Isolated capture entry for the IN-CHAT model picker on the Claude Code harness.
 *
 * Mounts the REAL ChatInput (so the composer's model chip is real) and the REAL
 * ModelEffortDropdown held open beside it. The model list is what Claude Code's
 * ACP bridge advertises: each row carries the backend's own `displayName`
 * ("Opus 5.5"), not the raw id, so the open list shows the native names.
 * Ids are passed through filterInteractiveModels exactly as the chat does.
 *
 * Query: ?theme=dark|light (maps to the app's kiro-dark / kiro-light tokens).
 */
import { createRoot } from 'react-dom/client'
import { useLayoutEffect, useRef, useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'

import ChatInput from '../src/components/ChatInput'
import ModelEffortDropdown from '../src/components/ModelEffortDropdown'
import { filterInteractiveModels } from '../src/hooks/useInteractiveModels'
import { initI18n } from '../src/i18n/all'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const claudeModels = [
  { name: 'auto', description: '' },
  { name: 'opus', displayName: 'Opus 5.5', description: 'For complex work and everyday tasks' },
  { name: 'fable', displayName: 'Fable 5.1', description: 'For your toughest challenges' },
  { name: 'sonnet', displayName: 'Sonnet 5.5', description: 'Most efficient for simpler tasks' },
  { name: 'claude-opus-5', displayName: 'Opus 5', description: 'Best for everyday, complex tasks' },
  { name: 'claude-haiku-4-5-20251001', displayName: 'Haiku 4.5', description: 'Fastest for quick answers' },
]

const ACTIVE = 'opus'

function InChatPicker() {
  const [value, setValue] = useState('')
  const [filter, setFilter] = useState('')
  const [queryClient] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }))
  const [store] = useState(() => configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  }))
  // Anchor the open list to the chip the composer really renders, as ChatEffortEvidence does.
  const composerRef = useRef<HTMLDivElement>(null)
  const [chipRect, setChipRect] = useState<DOMRect | null>(null)
  useLayoutEffect(() => {
    setChipRect(composerRef.current?.querySelector('[data-testid="composer-model-chip"]')?.getBoundingClientRect() ?? null)
  }, [])
  const groupedModels = filterInteractiveModels(claudeModels, [], [], false)
  const shownModels = groupedModels.filter(model => model.name.toLowerCase().includes(filter.toLowerCase()))

  return (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <div ref={composerRef} style={{ width: 680, position: 'absolute', left: 276, bottom: 72 }}>
          <ChatInput
            value={value}
            onChange={setValue}
            onSend={() => {}}
            providerId="acp"
            modelName={ACTIVE}
            onModelClick={() => {}}
            hasEffort={false}
          />
        </div>
        {chipRect && (
          <ModelEffortDropdown
            anchorRect={chipRect}
            dropdownRef={() => {}}
            inputRef={() => {}}
            models={shownModels}
            activeModel={ACTIVE}
            onSelectModel={() => {}}
            filter={filter}
            setFilter={setFilter}
            onClose={() => {}}
            hasEffort={false}
            slot="in-chat-picker-capture"
            currentEffort=""
            onListKeyDown={() => {}}
          />
        )}
      </Provider>
    </QueryClientProvider>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{
      position: 'relative', width: 1200, height: 640,
      background: 'var(--bg)', color: 'var(--text)',
      font: '13px system-ui, -apple-system, sans-serif',
    }}
  >
    <InChatPicker />
  </div>,
)
