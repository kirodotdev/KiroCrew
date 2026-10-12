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

vi.spyOn(api, 'chatSlotReasoningEffort').mockResolvedValue({ ok: true } as never)

/**
 * The top-down chat layout puts the composer at the top of the window, so the
 * model picker must open BELOW its chip there; above it, the box is clipped by
 * the window top. The default (bottom composer) still opens above the chip.
 */

const baseProps = {
  // A chip near the top of an 800 px window, as in the top-down layout.
  anchorRect: { right: 400, top: 200, bottom: 224 } as DOMRect,
  dropdownRef: React.createRef<HTMLDivElement>(),
  inputRef: React.createRef<HTMLInputElement>(),
  models: [{ name: 'auto', description: 'Default' }],
  activeModel: 'auto',
  onSelectModel: vi.fn(),
  filter: '',
  setFilter: vi.fn(),
  onClose: vi.fn(),
  hasEffort: false,
  slot: 'dashboard:1',
  currentEffort: '',
  onListKeyDown: vi.fn(),
}

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
}

describe('ModelEffortDropdown placement', () => {
  it('opens below the chip with the room below as its cap when openBelow is set', () => {
    window.innerHeight = 800
    wrap(<ModelEffortDropdown {...baseProps} openBelow />)
    const box = screen.getByRole('dialog')
    expect(box.style.top).toBe('228px')
    expect(box.style.bottom).toBe('')
    expect(box.style.maxHeight).toBe(`${800 - 224 - 12}px`)
  })

  it('opens above the chip by default', () => {
    window.innerHeight = 800
    wrap(<ModelEffortDropdown {...baseProps} />)
    const box = screen.getByRole('dialog')
    expect(box.style.bottom).toBe(`${800 - 200 + 4}px`)
    expect(box.style.top).toBe('')
    expect(box.style.maxHeight).toBe(`${200 - 12}px`)
  })
})
