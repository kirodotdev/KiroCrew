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
 * A Claude session whose model advertises `fast` (Fast mode) gets an on/off switch
 * in the picker, through the path the ultracode switch takes: the switch writes the
 * live session and shows the value the session reads back, and a session that
 * advertises no switch shows none. A switch that reads off while the harness says it
 * cannot be turned on (`fast_unavailable`) is drawn disabled with the reason as its hint.
 */

const SLOT = 'dashboard:1'
const CAPS_KEY = ['slot-selection-capabilities', SLOT]
const fastLabel = () => i18nT('components.fastModeToggle.label')
const ultracodeLabel = () => i18nT('components.ultracodeToggle.label')

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

type Caps = { known: boolean; fast?: boolean; ultracode?: boolean }

function wrap(ui: React.ReactElement, caps: Caps = { known: true, fast: false }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(CAPS_KEY, caps)
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  const tree = (node: React.ReactElement) => <Provider store={store}><QueryClientProvider client={qc}>{node}</QueryClientProvider></Provider>
  const view = render(tree(ui))
  return { qc, rerender: (node: React.ReactElement) => view.rerender(tree(node)) }
}

/** The text of the hint row a switch is described by. */
function hintOf(toggle: HTMLElement) {
  return document.getElementById(toggle.getAttribute('aria-describedby') ?? '')?.textContent
}

afterEach(() => { vi.restoreAllMocks() })

describe('ModelEffortDropdown — Fast mode switch', () => {
  it('writes the switch and keeps the value the session read back', async () => {
    const write = vi.spyOn(api, 'chatSlotFastMode').mockResolvedValue({ ok: true, fast: true })
    const { qc } = wrap(<ModelEffortDropdown {...baseProps} fastMode={false} />)
    const toggle = screen.getByRole('switch', { name: fastLabel() })
    expect(toggle.getAttribute('aria-checked')).toBe('false')

    fireEvent.click(toggle)

    await waitFor(() => expect(write).toHaveBeenCalledWith(SLOT, true))
    await waitFor(() => expect(qc.getQueryData<Caps>(CAPS_KEY)?.fast).toBe(true))
  })

  it('keeps the read-back value when a poll that read the old value lands after the write', async () => {
    vi.spyOn(api, 'chatSlotFastMode').mockResolvedValue({ ok: true, fast: true })
    const { qc } = wrap(<ModelEffortDropdown {...baseProps} fastMode={false} />)
    let finishPoll: (caps: unknown) => void = () => {}
    const poll = qc.prefetchQuery({ queryKey: CAPS_KEY, queryFn: () => new Promise(resolve => { finishPoll = resolve }) })

    fireEvent.click(screen.getByRole('switch', { name: fastLabel() }))
    await waitFor(() => expect(qc.getQueryData<Caps>(CAPS_KEY)?.fast).toBe(true))
    finishPoll({ known: true, fast: false })
    await poll

    expect(qc.getQueryData<Caps>(CAPS_KEY)?.fast).toBe(true)
  })

  it('lands a write on the chat that made it when the picker moves to another chat mid-write', async () => {
    let finishWrite: (r: { ok: boolean; fast: boolean }) => void = () => {}
    const write = vi.spyOn(api, 'chatSlotFastMode').mockImplementation(() => new Promise(resolve => { finishWrite = resolve }))
    const OTHER_KEY = ['slot-selection-capabilities', 'dashboard:2']
    const { qc, rerender } = wrap(<ModelEffortDropdown {...baseProps} fastMode={false} />)
    qc.setQueryData(OTHER_KEY, { known: true, fast: false })

    fireEvent.click(screen.getByRole('switch', { name: fastLabel() }))
    await waitFor(() => expect(write).toHaveBeenCalledWith(SLOT, true))
    rerender(<ModelEffortDropdown {...baseProps} slot="dashboard:2" fastMode={false} />)
    finishWrite({ ok: true, fast: true })

    await waitFor(() => expect(qc.getQueryData<Caps>(CAPS_KEY)?.fast).toBe(true))
    expect(qc.getQueryData<Caps>(OTHER_KEY)?.fast).toBe(false)
  })

  it('draws a switch the harness says cannot be turned on disabled, with the reason as its hint', () => {
    const write = vi.spyOn(api, 'chatSlotFastMode')
    const reason = 'not available on the free plan'
    wrap(<ModelEffortDropdown {...baseProps} fastMode={false} fastUnavailable={reason} />)
    const toggle = screen.getByRole('switch', { name: fastLabel() })
    expect(toggle.getAttribute('aria-disabled')).toBe('true')
    const hint = hintOf(toggle)
    expect(hint).toBe(i18nT('components.fastModeToggle.unavailable', { reason }))
    expect(hint).toContain(reason)

    fireEvent.click(toggle)

    expect(write).not.toHaveBeenCalled()
  })

  it('names no reason the harness did not give', () => {
    wrap(<ModelEffortDropdown {...baseProps} fastMode={false} fastUnavailable="" />)
    const toggle = screen.getByRole('switch', { name: fastLabel() })
    expect(toggle.getAttribute('aria-disabled')).toBe('true')
    expect(hintOf(toggle)).toBe(i18nT('components.fastModeToggle.unavailable_no_reason'))
  })

  it('keeps a switch that reads on writable, so it can always be turned off', async () => {
    const write = vi.spyOn(api, 'chatSlotFastMode').mockResolvedValue({ ok: true, fast: false })
    wrap(<ModelEffortDropdown {...baseProps} fastMode fastUnavailable="not available on the free plan" />, { known: true, fast: true })
    const toggle = screen.getByRole('switch', { name: fastLabel() })
    expect(toggle.getAttribute('aria-disabled')).toBeNull()
    expect(hintOf(toggle)).toBe(i18nT('components.fastModeToggle.hint'))

    fireEvent.click(toggle)

    await waitFor(() => expect(write).toHaveBeenCalledWith(SLOT, false))
  })

  it('shows no switch when the session advertises none', () => {
    wrap(<ModelEffortDropdown {...baseProps} ultracode={false} />, { known: true, ultracode: false })
    expect(screen.queryByRole('switch', { name: fastLabel() })).toBeNull()
    expect(screen.getByRole('switch', { name: ultracodeLabel() })).toBeTruthy()
  })

  it('sits below the ultracode switch, and each writes only its own field', async () => {
    const fast = vi.spyOn(api, 'chatSlotFastMode').mockResolvedValue({ ok: true, fast: true })
    const ultracode = vi.spyOn(api, 'chatSlotUltracode')
    const { qc } = wrap(
      <ModelEffortDropdown {...baseProps} ultracode={false} fastMode={false} />,
      { known: true, ultracode: false, fast: false },
    )
    const names = screen.getAllByRole('switch').map(el => el.getAttribute('aria-label'))
    expect(names).toEqual([ultracodeLabel(), fastLabel()])

    fireEvent.click(screen.getByRole('switch', { name: fastLabel() }))

    await waitFor(() => expect(qc.getQueryData<Caps>(CAPS_KEY)).toEqual({ known: true, ultracode: false, fast: true }))
    expect(fast).toHaveBeenCalledWith(SLOT, true)
    expect(ultracode).not.toHaveBeenCalled()
  })
})
