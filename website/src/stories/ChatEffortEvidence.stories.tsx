import { useLayoutEffect, useRef, useState } from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'

import ChatInput from '../components/ChatInput'
import ErrorNotice from '../components/ErrorNotice'
import ModelEffortDropdown from '../components/ModelEffortDropdown'
import { filterInteractiveModels, shouldSeparateModelEffort } from '../hooks/useInteractiveModels'
import { i18nT } from '../i18n/t'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/** Visual fixture: real composer controls with representative ACP capability data.
 *  Model + effort are ONE control: the effort slider lives inside the model
 *  picker (docs/decisions/2026-06-14-chat-composer-model-and-effort-are-one-control.md). */
type State = 'default' | 'selected' | 'compact' | 'inherited-default' | 'tiny-split-pane' | 'tiny-split-pane-models' | 'models' | 'models-configured-default' | 'models-configured-default-override' | 'read-error'

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
  // 'inherited-default': the slot pinned no model (it runs the agent's
  // default) AND runs at the configured default effort. The chip already says
  // "default" for the model, so the effort keeps its bare level ("High"), not
  // "Default · High" -- the accessible name still carries the full form.
  const inheritedDefault = state === 'inherited-default'
  // 'tiny-split-pane-models' is the short pane with its picker OPEN: the pane
  // hangs from the viewport top so its chip sits ~300px down, and the picker
  // is capped to that space (the model list, not the filter, shrinks).
  const tinySplitPaneModels = state === 'tiny-split-pane-models'
  const tinySplitPane = state === 'tiny-split-pane' || tinySplitPaneModels
  const readError = state === 'read-error'
  // The open picker over the composer. 'models' carries a per-slot override
  // with NO configured default (toggle reads "Use default effort");
  // 'models-configured-default' runs AT the configured default, so the toggle
  // names its level and the chip says "Default · High";
  // 'models-configured-default-override' has a configured default but the
  // slot pinned elsewhere (switch off), which is the one state where the
  // "Default effort" marker sits on the track beside the switch.
  const configuredDefault = state === 'models-configured-default' || state === 'models-configured-default-override' || inheritedDefault
  const overridesDefault = state === 'models-configured-default-override'
  const pickerOpen = !inheritedDefault && (state === 'models' || configuredDefault || tinySplitPaneModels)
  const selected = state === 'selected' || state === 'compact' || state === 'models' || tinySplitPaneModels
  // The short pane's picker anchors to the chip the pane actually renders, so
  // the cap in the frame is the one the product computes for that geometry.
  const paneRef = useRef<HTMLDivElement>(null)
  const [chipRect, setChipRect] = useState<DOMRect | null>(null)
  useLayoutEffect(() => {
    if (!tinySplitPaneModels) return
    setChipRect(paneRef.current?.querySelector('[data-testid="composer-model-chip"]')?.getBoundingClientRect() ?? null)
  }, [tinySplitPaneModels])
  // What the chip is handed: ChatPage/ChatPane pass the effort IN FORCE
  // (per-slot override, else the configured default) plus whether that value
  // IS the configured default, so a slot running at a configured "high" shows
  // "Default · High" on the chip and a pinned "high" shows "High" -- never a
  // bare "· Default". The fixture must hand the chip the same resolved pair or
  // the screenshot shows a state the product never renders.
  const chipEffort = overridesDefault ? 'medium' : selected || configuredDefault ? 'high' : ''
  const chipEffortIsDefault = configuredDefault && !overridesDefault
  const pairIds = shouldSeparateModelEffort(true, advertisedModels)
  const groupedModels = filterInteractiveModels(advertisedModels, [], [], pairIds)
  const shownModels = groupedModels.filter(model => model.name.toLowerCase().includes(filter.toLowerCase()))

  return (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <div ref={paneRef} style={{
          width: tinySplitPane ? 157 : compact ? 312 : 680,
          position: 'absolute', left: tinySplitPane ? 24 : compact ? 460 : 276,
          ...(tinySplitPaneModels ? { top: 24 } : { bottom: 72 }),
          ...(tinySplitPane ? { height: tinySplitPaneModels ? 300 : 420, border: '1px solid var(--border)', display: 'flex', flexDirection: 'column' as const, justifyContent: 'space-between' } : {}),
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
            modelIsInheritedDefault={inheritedDefault}
            onModelClick={() => {}}
            reasoningEffort={chipEffort}
            effortIsDefault={chipEffortIsDefault}
            hasEffort={!readError}
          />
        </div>
        {pickerOpen && (!tinySplitPaneModels || chipRect) && (
          <ModelEffortDropdown
            anchorRect={tinySplitPaneModels && chipRect ? chipRect : new DOMRect(686, 760, 190, 28)}
            dropdownRef={() => {}}
            inputRef={() => {}}
            models={shownModels}
            activeModel="gpt-6-sol"
            onSelectModel={() => {}}
            filter={filter}
            setFilter={setFilter}
            onClose={() => {}}
            hasEffort
            slot="evidence-preview"
            currentEffort={overridesDefault ? 'medium' : configuredDefault ? '' : 'high'}
            defaultEffort={configuredDefault ? 'high' : ''}
            effortLevelsOverride={['low', 'medium', 'high']}
            onListKeyDown={() => {}}
          />
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
export const InheritedDefault: Story = { args: { state: 'inherited-default' } }
export const TinySplitPane: Story = { args: { state: 'tiny-split-pane' } }
export const TinySplitPaneModels: Story = { args: { state: 'tiny-split-pane-models' } }
export const GroupedModels: Story = { args: { state: 'models' } }
export const GroupedModelsConfiguredDefault: Story = { args: { state: 'models-configured-default' } }
export const GroupedModelsConfiguredDefaultOverride: Story = { args: { state: 'models-configured-default-override' } }
export const ReadError: Story = { args: { state: 'read-error' } }
