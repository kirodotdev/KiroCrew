import { useState } from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'

import ChatInput from '../components/ChatInput'
import ErrorNotice from '../components/ErrorNotice'
import ModelEffortDropdown from '../components/ModelEffortDropdown'
import ReasoningEffortDropdown from '../components/ReasoningEffortDropdown'
import { filterInteractiveModels, shouldSeparateModelEffort } from '../hooks/useInteractiveModels'
import { i18nT } from '../i18n/t'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/** Visual fixture: real composer controls with representative ACP capability data. */
type State = 'default' | 'selected' | 'compact' | 'tiny-split-pane' | 'models' | 'effort-menu' | 'read-error'

const advertisedModels = [
  { name: 'gpt-6-sol[low]', description: '' },
  { name: 'gpt-6-sol[medium]', description: '' },
  { name: 'gpt-6-sol[high]', description: '' },
  { name: 'gpt-6-sol', description: 'Workhorse model for coding and everyday work.' },
  { name: 'gpt-6-astra[medium]', description: 'Frontier reasoning for difficult work.' },
  { name: 'gpt-6-astra[high]', description: 'Frontier reasoning for difficult work.' },
]

function ChatEffortEvidence({ state }: { state: State }) {
  const [value, setValue] = useState('')
  const [filter, setFilter] = useState('')
  const [queryClient] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }))
  const [store] = useState(() => configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  }))
  const compact = state === 'compact'
  const tinySplitPane = state === 'tiny-split-pane'
  const readError = state === 'read-error'
  const selected = state === 'selected' || state === 'compact' || state === 'effort-menu'
  const pairIds = shouldSeparateModelEffort(true, advertisedModels)
  const groupedModels = filterInteractiveModels(advertisedModels, [], [], pairIds)
  const shownModels = groupedModels.filter(model => model.name.toLowerCase().includes(filter.toLowerCase()))

  return (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <div style={{
          width: tinySplitPane ? 157 : compact ? 312 : 680,
          position: 'absolute', left: tinySplitPane ? 24 : compact ? 460 : 276,
          bottom: 72,
          ...(tinySplitPane ? { height: 420, border: '1px solid var(--border)', display: 'flex', flexDirection: 'column' as const, justifyContent: 'space-between' } : {}),
        }}>
          {tinySplitPane && <div className="border-b border-border px-2 py-1 text-[11px] text-muted">Split pane · 157px</div>}
          {/* No hand-off: navigating away would discard the unsent composer draft. */}
          {readError && (
            <ErrorNotice
              message={i18nT('pages.chatPage.effort_options_unavailable')}
              className="mb-4"
              testId="effort-capabilities-error"
            />
          )}
          <ChatInput
            value={value}
            onChange={setValue}
            onSend={() => {}}
            providerId="acp"
            modelName="gpt-6-sol"
            onModelClick={() => {}}
            reasoningEffort={selected ? 'high' : ''}
            onReasoningEffortClick={readError ? undefined : () => {}}
            separateEffort
          />
        </div>
        {state === 'models' && (
          <ModelEffortDropdown
            anchorRect={new DOMRect(686, 760, 190, 28)}
            dropdownRef={() => {}}
            inputRef={() => {}}
            models={shownModels}
            activeModel="gpt-6-sol"
            onSelectModel={() => {}}
            filter={filter}
            setFilter={setFilter}
            onListKeyDown={() => {}}
          />
        )}
        {state === 'effort-menu' && (
          <div style={{ position: 'absolute', left: 660, bottom: 180 }}>
            <ReasoningEffortDropdown
              slot="evidence-preview"
              currentEffort="high"
              levelsOverride={['low', 'medium', 'high', 'xhigh', 'max']}
              onClose={() => {}}
            />
          </div>
        )}
      </Provider>
    </QueryClientProvider>
  )
}

const meta = {
  title: 'Evidence/ACP chat effort',
  component: ChatEffortEvidence,
  args: { state: 'default' },
} satisfies Meta<typeof ChatEffortEvidence>

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {}
export const Selected: Story = { args: { state: 'selected' } }
export const Compact: Story = { args: { state: 'compact' } }
export const TinySplitPane: Story = { args: { state: 'tiny-split-pane' } }
export const GroupedModels: Story = { args: { state: 'models' } }
export const EffortMenu: Story = { args: { state: 'effort-menu' } }
export const ReadError: Story = { args: { state: 'read-error' } }
