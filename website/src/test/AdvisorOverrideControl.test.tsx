import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import AdvisorOverrideControl from '../components/AdvisorOverrideControl'
import ModelEffortDropdown from '../components/ModelEffortDropdown'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'

const advisorOverride = vi.spyOn(api, 'chatSlotAdvisorOverride')
const kirocrewConfig = vi.spyOn(api, 'kirocrewConfig')

function storeFor(value: 'inherit' | 'on' | 'off' = 'inherit') {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        slots: [{ key: 's1', messages: 0, running: false, advisor_override: value }],
        unreadSlots: [], refreshTrigger: 0, subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function wrap(ui: React.ReactElement, value: 'inherit' | 'on' | 'off' = 'inherit') {
  const store = storeFor(value)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return Object.assign(
    render(<Provider store={store}><QueryClientProvider client={client}>{ui}</QueryClientProvider></Provider>),
    { store },
  )
}

async function choose(label: string) {
  // A native <select>: the listbox is browser chrome, never a portaled element,
  // so choosing cannot fire a document click outside the model popover.
  const select = (await screen.findByRole('combobox', { name: 'Advisor mode' })) as HTMLSelectElement
  const option = Array.from(select.options).find(o => o.textContent === label)
  if (!option) throw new Error(`no option ${label}`)
  fireEvent.change(select, { target: { value: option.value } })
}

describe('AdvisorOverrideControl', () => {
  beforeEach(() => {
    advisorOverride.mockReset().mockResolvedValue({ ok: true, advisor_override: 'on' })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    kirocrewConfig.mockReset().mockResolvedValue({ advisor: { enabled: false } } as any)
  })

  it('offers inherit, on, and off and writes the persisted value into the slot row', async () => {
    const { store } = wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)

    const select = (await screen.findByRole('combobox', { name: 'Advisor mode' })) as HTMLSelectElement
    // once the config query resolves, the Inherit option names its effective value
    await screen.findByRole('option', { name: 'Inherit (Disabled)' })
    expect(Array.from(select.options).map(o => o.textContent)).toEqual(['Inherit (Disabled)', 'Enabled', 'Disabled'])
    await choose('Enabled')

    await waitFor(() => expect(advisorOverride).toHaveBeenCalledWith('s1', 'on'))
    await waitFor(() => expect(store.getState().dashboard.slots[0].advisor_override).toBe('on'))
  })

  it('explains the feature for the untouched default without claiming a state', async () => {
    // Off by default: the untouched popover says what the Advisor IS and where
    // to enable it (no state sentence, no nudge). Once the user picks Disabled
    // themselves, the helper names that state.
    wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)
    await screen.findByRole('combobox', { name: 'Advisor mode' })
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled())
    expect(screen.queryByText('Not reviewing.')).not.toBeInTheDocument()
    expect(screen.getByText(/separate reviewer session can review this session/)).toBeInTheDocument()

    advisorOverride.mockResolvedValue({ ok: true, advisor_override: 'off' })
    await choose('Disabled')
    await screen.findByText('Not reviewing.')
  })

  it('shows no inherit(off) explainer until the global setting has resolved', async () => {
    let resolve!: (v: unknown) => void
    kirocrewConfig.mockReturnValue(new Promise(r => (resolve = r)))
    wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)
    // Nothing renders while the global setting is pending: no row that would
    // later relabel or vanish and spring the popover's height.
    expect(screen.queryByRole('combobox', { name: 'Advisor mode' })).not.toBeInTheDocument()
    expect(screen.queryByText(/separate reviewer session can review this session/)).not.toBeInTheDocument()
    resolve({ advisor: { enabled: true } })
    await screen.findByText(/separate reviewer session reviews this session/)
    expect(screen.queryByText(/separate reviewer session can review this session/)).not.toBeInTheDocument()
  })

  it('renders nothing when the gateway backend is not kiro-cli', async () => {
    // The reviewer cannot run on another backend: a disabled row with a
    // directive is daily chrome about a setting most users will never change,
    // so the control is absent. The 409 path still carries the reason for a
    // request that races a backend switch.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    kirocrewConfig.mockResolvedValue({ advisor: { enabled: false }, agent: { acp_backend: 'claude' } } as any)
    const { container } = wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByRole('combobox', { name: 'Advisor mode' })).not.toBeInTheDocument())
    expect(container.textContent).toBe('')
    expect(advisorOverride).not.toHaveBeenCalled()
  })

  it('names the setting to change when the backend refuses Enabled', async () => {
    const { ApiError } = await import('../api/apiError')
    advisorOverride.mockRejectedValueOnce(
      new ApiError(409, 'conflict', JSON.stringify({ error: 'x', code: 'advisor_unavailable' })),
    )
    wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)

    await choose('Enabled')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The Advisor needs the kiro-cli agent backend. Switch Settings → Developer → Agent Backend to kiro-cli, or leave the Advisor off.',
    )
  })

  it('names the sandbox when the host cannot mask credentials for the reviewer', async () => {
    const { ApiError } = await import('../api/apiError')
    advisorOverride.mockRejectedValueOnce(
      new ApiError(409, 'conflict', JSON.stringify({ error: 'x', code: 'advisor_sandbox_unavailable' })),
    )
    wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)

    await choose('Enabled')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The Advisor needs a sandbox that hides your credentials, and this computer cannot provide one.',
    )
  })

  it('renders a native select so choosing never portals outside the model popover', async () => {
    // The model popover closes on any document click outside its container
    // (useFilteredDropdown). A Radix listbox portals to <body>, so picking an
    // option there would dismiss the popover and hide the outcome; the native
    // element keeps the whole interaction inside the container.
    const { container } = wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)
    const select = await screen.findByRole('combobox', { name: 'Advisor mode' })
    expect(select.tagName).toBe('SELECT')
    expect(container.contains(select)).toBe(true)
  })

  it('rolls back and renders ErrorNotice when the write fails', async () => {
    // The server's reason (e.g. the reviewer is unavailable under the selected
    // backend) must reach the user, or they retry a choice that cannot stick.
    advisorOverride.mockRejectedValueOnce(new Error('reviewer runs on the kiro-cli agent backend only'))
    wrap(<AdvisorOverrideControl slot="s1" currentOverride="inherit" />)

    await choose('Enabled')

    // Unknown failures show the plain line; the raw body goes to the console,
    // never into the popover.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not update Advisor.')
    expect(alert).not.toHaveTextContent('kiro-cli agent backend only')
    await waitFor(() => expect((screen.getByRole('combobox', { name: 'Advisor mode' }) as HTMLSelectElement).value).toBe('inherit'))
  })

  it('lives in the same per-session model panel as reasoning effort', async () => {
    wrap(
      <ModelEffortDropdown
        anchorRect={{ right: 400, top: 300 } as DOMRect}
        dropdownRef={{ current: null }}
        inputRef={{ current: null }}
        models={[{ name: 'auto' }]}
        activeModel="auto"
        onSelectModel={vi.fn()}
        filter=""
        setFilter={vi.fn()}
        onClose={vi.fn()}
        hasEffort
        slot="s1"
        currentEffort="high"
        currentAdvisorOverride="off"
        onListKeyDown={vi.fn()}
      />,
      'off',
    )

    expect(screen.getByRole('button', { name: /^Reasoning/ })).toBeInTheDocument()
    expect(((await screen.findByRole('combobox', { name: 'Advisor mode' })) as HTMLSelectElement).value).toBe('off')
    // with the global default off and Inherit selected nothing reviews, and the
    // helper must not claim active review
    expect(screen.getByText('Not reviewing.')).toBeInTheDocument()
  })
})
