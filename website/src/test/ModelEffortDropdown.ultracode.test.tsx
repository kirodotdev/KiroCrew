import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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

/**
 * A Claude session whose model advertises `ultracode` gets an on/off switch
 * under the effort slider. The switch writes the live session and shows the value
 * the session reads back; a session that advertises no switch shows none.
 */

const SLOT = 'dashboard:1'
const CAPS_KEY = ['slot-selection-capabilities', SLOT]

const baseProps = {
  anchorRect: { right: 400, top: 300 } as DOMRect,
  dropdownRef: React.createRef<HTMLDivElement>(),
  inputRef: React.createRef<HTMLInputElement>(),
  models: [{ name: 'claude-opus-4.8' }],
  activeModel: 'claude-opus-4.8',
  onSelectModel: vi.fn(),
  filter: '',
  setFilter: vi.fn(),
  onClose: vi.fn(),
  hasEffort: false,
  slot: SLOT,
  currentEffort: '',
  onListKeyDown: vi.fn(),
}

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(CAPS_KEY, { known: true, ultracode: false })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  const tree = (node: React.ReactElement) => <Provider store={store}><QueryClientProvider client={qc}>{node}</QueryClientProvider></Provider>
  const view = render(tree(ui))
  return { qc, rerender: (node: React.ReactElement) => view.rerender(tree(node)) }
}

afterEach(() => { vi.restoreAllMocks() })

describe('ModelEffortDropdown — ultracode switch', () => {
  it('writes the switch and keeps the value the session read back', async () => {
    const write = vi.spyOn(api, 'chatSlotUltracode').mockResolvedValue({ ok: true, ultracode: true })
    const { qc } = wrap(<ModelEffortDropdown {...baseProps} ultracode={false} />)
    const toggle = screen.getByRole('switch', { name: i18nT('components.ultracodeToggle.label') })
    expect(toggle.getAttribute('aria-checked')).toBe('false')

    fireEvent.click(toggle)

    await waitFor(() => expect(write).toHaveBeenCalledWith(SLOT, true))
    await waitFor(() => expect(qc.getQueryData<{ ultracode?: boolean }>(CAPS_KEY)?.ultracode).toBe(true))
  })

  it('keeps the read-back value when a poll that read the old value lands after the write', async () => {
    vi.spyOn(api, 'chatSlotUltracode').mockResolvedValue({ ok: true, ultracode: true })
    const { qc } = wrap(<ModelEffortDropdown {...baseProps} ultracode={false} />)
    let finishPoll: (caps: unknown) => void = () => {}
    const poll = qc.prefetchQuery({ queryKey: CAPS_KEY, queryFn: () => new Promise(resolve => { finishPoll = resolve }) })

    fireEvent.click(screen.getByRole('switch', { name: i18nT('components.ultracodeToggle.label') }))
    await waitFor(() => expect(qc.getQueryData<{ ultracode?: boolean }>(CAPS_KEY)?.ultracode).toBe(true))
    finishPoll({ known: true, ultracode: false })
    await poll

    expect(qc.getQueryData<{ ultracode?: boolean }>(CAPS_KEY)?.ultracode).toBe(true)
  })

  it('lands a write on the chat that made it when the picker moves to another chat mid-write', async () => {
    let finishWrite: (r: { ok: boolean; ultracode: boolean }) => void = () => {}
    const write = vi.spyOn(api, 'chatSlotUltracode').mockImplementation(() => new Promise(resolve => { finishWrite = resolve }))
    const OTHER_KEY = ['slot-selection-capabilities', 'dashboard:2']
    const { qc, rerender } = wrap(<ModelEffortDropdown {...baseProps} ultracode={false} />)
    qc.setQueryData(OTHER_KEY, { known: true, ultracode: false })

    fireEvent.click(screen.getByRole('switch', { name: i18nT('components.ultracodeToggle.label') }))
    await waitFor(() => expect(write).toHaveBeenCalledWith(SLOT, true))
    rerender(<ModelEffortDropdown {...baseProps} slot="dashboard:2" ultracode={false} />)
    finishWrite({ ok: true, ultracode: true })

    await waitFor(() => expect(qc.getQueryData<{ ultracode?: boolean }>(CAPS_KEY)?.ultracode).toBe(true))
    expect(qc.getQueryData<{ ultracode?: boolean }>(OTHER_KEY)?.ultracode).toBe(false)
  })

  it('shows no switch when the session advertises none', () => {
    wrap(<ModelEffortDropdown {...baseProps} />)
    expect(screen.queryByRole('switch', { name: i18nT('components.ultracodeToggle.label') })).toBeNull()
  })
})
