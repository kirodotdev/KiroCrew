import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'
import ModelEffortDropdown from '../components/ModelEffortDropdown'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'

vi.spyOn(api, 'chatSlotReasoningEffort').mockResolvedValue({ ok: true } as never)

/**
 * #11546: on `auto` the kiro backend refuses an effort level until a specific
 * model is selected, so the picker offers no slider. It must say that effort
 * does not apply there, instead of leaving the configured default looking live.
 */

const baseProps = {
  anchorRect: { right: 400, top: 300 } as DOMRect,
  dropdownRef: React.createRef<HTMLDivElement>(),
  inputRef: React.createRef<HTMLInputElement>(),
  models: [{ name: 'auto', description: 'Default' }, { name: 'claude-opus-4.8' }],
  activeModel: 'auto',
  onSelectModel: vi.fn(),
  filter: '',
  setFilter: vi.fn(),
  onClose: vi.fn(),
  hasEffort: false,
  slot: 'dashboard:1',
  currentEffort: '',
  defaultEffort: 'low',
  onListKeyDown: vi.fn(),
}

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
}

describe('ModelEffortDropdown — effort on auto', () => {
  it('says effort does not apply on auto, and shows no slider', () => {
    wrap(<ModelEffortDropdown {...baseProps} effortOffOnAuto />)
    const note = screen.getByTestId('effort-off-on-auto')
    expect(note.textContent).toBe(i18nT('components.reasoningEffortDropdown.effort_off_on_auto'))
    expect(screen.queryByRole('slider')).toBeNull()
  })

  it('shows no note when the session is not on auto', () => {
    wrap(<ModelEffortDropdown {...baseProps} />)
    expect(screen.queryByTestId('effort-off-on-auto')).toBeNull()
  })

  it('shows no note while an effort control is offered', () => {
    wrap(<ModelEffortDropdown {...baseProps} activeModel="claude-opus-4.8" hasEffort effortOffOnAuto />)
    expect(screen.queryByTestId('effort-off-on-auto')).toBeNull()
  })
})
