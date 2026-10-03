// The composer's per-chat AI backend picker and the model list it keys.
//
// Pinned here:
// - the picker offers only selectable, startable backends and hides itself
//   when there is nothing to choose
// - it is locked while a turn runs, like the model picker
// - a pick reports the backend id ('' for Kiro); "Default backend" reports null
// - a pick the list no longer offers still shows as itself
// - "Default" names the configured backend once the config loads
// - a pick the gateway degraded to Kiro says so on its row
// - a failed probe renders an error, except the permanent 403/404 answers
// - the model list for a picked backend is fetched from THAT backend on first
//   mount, under its own cache entry, and never touches the configured
//   backend's entry
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import BackendPicker, { BackendPickerNotices, pickableBackends, switchClearedModelPin, switchNoticeKey } from '../components/BackendPicker'
import { api, type AcpBackendProbe } from '../api/client'
import { ApiError } from '../api/apiError'

const fetchAvailableModels = vi.fn(async (backend?: string) => [{ name: backend === 'kas' ? 'kas-model' : 'kiro-model', description: '' }])
vi.mock('../providers', () => ({
  useProvider: () => ({ id: 'acp', fetchAvailableModels }),
}))

// The themed select is a Radix popup; stand in a plain list of buttons so the
// tests read the options and the mapping, not Radix's portal mechanics.
vi.mock('../components/SimpleSelect', () => ({
  default: (p: { options: string[]; optionLabels?: string[]; value: string; disabled?: boolean; onChange: (v: string) => void }) => (
    <div data-testid="select" data-value={p.value} data-disabled={String(!!p.disabled)}>
      {p.options.map((o, i) => <button key={o} data-opt={o} data-label={p.optionLabels?.[i]} onClick={() => p.onChange(o)}>{o}</button>)}
    </div>
  ),
}))

const { useAvailableModelsQuery } = await import('../hooks/useAvailableModels')

function row(id: string, over: Partial<AcpBackendProbe> = {}): AcpBackendProbe {
  return {
    id,
    policy_id: id || 'kiro',
    selectable: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    restart_required: false,
    ...over,
  } as AcpBackendProbe
}

function wrap(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

function renderPicker(props: { value: string | null; degraded?: boolean; disabled?: boolean; onChange?: (b: string | null) => void }, rows: AcpBackendProbe[], configured?: string) {
  vi.spyOn(api, 'acpBackends').mockResolvedValue({ backends: rows })
  if (configured === undefined) vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('no config'))
  else vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { acp_backend: configured } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <BackendPicker value={props.value} degraded={props.degraded} disabled={!!props.disabled} onChange={props.onChange ?? (() => {})} />,
    { wrapper: wrap(client) },
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  fetchAvailableModels.mockClear()
})

/** The picker and its notices, mounted together as the composer mounts them. */
function Both({ value, switchError = null, onDismissSwitchError }: { value: string | null; switchError?: string | null; onDismissSwitchError?: () => void }) {
  return (
    <>
      <BackendPicker value={value} disabled={false} onChange={() => {}} />
      <BackendPickerNotices value={value} switchError={switchError} onDismissSwitchError={onDismissSwitchError} />
    </>
  )
}

describe('pickableBackends', () => {
  it('drops unselectable, missing and restart-pending backends', () => {
    const rows = [row(''), row('kas'), row('claude', { selectable: false }), row('codex', { installed: 'missing' }), row('pi', { restart_required: true })]
    expect(pickableBackends(rows).map(r => r.id)).toEqual(['', 'kas'])
  })
})

const opts = () => Array.from(screen.getByTestId('select').querySelectorAll('button')).map(b => b.getAttribute('data-opt'))
const labelOf = (o: string) => screen.getByTestId('select').querySelector(`[data-opt="${o}"]`)!.getAttribute('data-label')
const pick = (o: string) => fireEvent.click(screen.getByTestId('select').querySelector(`[data-opt="${o}"]`)!)

describe('switchClearedModelPin', () => {
  it('reports a pinned model the switch cleared', () => {
    expect(switchClearedModelPin(true, 'claude-opus-4.8')).toBe(true)
  })
  it('stays quiet when nothing was pinned or nothing changed', () => {
    expect(switchClearedModelPin(true, '')).toBe(false)
    expect(switchClearedModelPin(true, 'auto')).toBe(false)
    expect(switchClearedModelPin(false, 'claude-opus-4.8')).toBe(false)
    expect(switchClearedModelPin(undefined, 'claude-opus-4.8')).toBe(false)
  })
})

describe('switchNoticeKey', () => {
  it('says every real switch starts a fresh session, and names a cleared pin', () => {
    expect(switchNoticeKey(true, '')).toBe('components.backendPicker.switched_fresh_session')
    expect(switchNoticeKey(true, 'auto')).toBe('components.backendPicker.switched_fresh_session')
    expect(switchNoticeKey(true, 'claude-opus-4.8')).toBe('components.backendPicker.model_pin_cleared')
  })
  it('stays quiet when nothing changed', () => {
    expect(switchNoticeKey(false, 'claude-opus-4.8')).toBeNull()
    expect(switchNoticeKey(undefined, '')).toBeNull()
  })
})

describe('BackendPicker', () => {
  it('offers the startable backends and reports the picked id', async () => {
    const onChange = vi.fn()
    renderPicker({ value: null, onChange }, [row(''), row('kas'), row('claude', { selectable: false })])
    await screen.findByTestId('select')
    expect(opts()).toEqual(['__default__', '__kiro__', 'kas'])
    pick('kas')
    expect(onChange).toHaveBeenLastCalledWith('kas')
    pick('__kiro__')
    expect(onChange).toHaveBeenLastCalledWith('')
    pick('__default__')
    expect(onChange).toHaveBeenLastCalledWith(null)
  })

  it('is locked while a turn runs', async () => {
    renderPicker({ value: 'kas', disabled: true }, [row(''), row('kas')])
    const select = await screen.findByTestId('select')
    expect(select.getAttribute('data-disabled')).toBe('true')
    expect(select.getAttribute('data-value')).toBe('kas')
  })

  it('shows a Kiro pick as Kiro, not as no pick', async () => {
    renderPicker({ value: '' }, [row(''), row('kas')])
    expect((await screen.findByTestId('select')).getAttribute('data-value')).toBe('__kiro__')
  })

  it('renders nothing when there is nothing to choose', async () => {
    renderPicker({ value: null }, [row('')])
    await waitFor(() => expect(api.acpBackends).toHaveBeenCalled())
    expect(screen.queryByTestId('composer-backend-picker')).toBeNull()
  })

  it('names the configured backend on the Default row', async () => {
    renderPicker({ value: null }, [row(''), row('kas')], 'kas')
    await screen.findByTestId('select')
    await waitFor(() => expect(labelOf('__default__')).toBe('Default (KAS (kiro-agent))'))
  })

  it('keeps the plain Default label until the config has loaded', async () => {
    renderPicker({ value: null }, [row(''), row('kas')])
    await screen.findByTestId('select')
    expect(labelOf('__default__')).toBe('Default backend')
  })

  it('says a degraded pick is running on Kiro', async () => {
    renderPicker({ value: 'kas', degraded: true }, [row(''), row('kas')])
    await screen.findByTestId('select')
    expect(labelOf('kas')).toBe('KAS (kiro-agent) (unavailable, using Kiro CLI)')
    // Truncation-proof: the chip's icon carries the degraded state on its own.
    expect(screen.getByTestId('composer-backend-picker-degraded').getAttribute('aria-label')).toBe('KAS (kiro-agent) (unavailable, using Kiro CLI)')
    cleanup()
    renderPicker({ value: 'kas' }, [row(''), row('kas')])
    await screen.findByTestId('select')
    expect(labelOf('kas')).toBe('KAS (kiro-agent)')
    expect(screen.queryByTestId('composer-backend-picker-degraded')).toBeNull()
  })

  it('says so when the backend probe fails instead of vanishing', async () => {
    vi.spyOn(api, 'acpBackends').mockRejectedValue(new ApiError(500, 'boom'))
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('no config'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<Both value={null} />, { wrapper: wrap(client) })
    expect(await screen.findByTestId('composer-backend-picker-error')).toBeTruthy()
    expect(screen.getByText("Couldn't load the AI backends")).toBeTruthy()
    // The failure is reported on the notices' own row, not inside the capped picker.
    expect(screen.queryByTestId('composer-backend-picker')).toBeNull()
  })

  it('says so when the config read fails, so Default is not shown as if it loaded', async () => {
    vi.spyOn(api, 'acpBackends').mockResolvedValue({ backends: [row(''), row('kas')] })
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new ApiError(500, 'boom'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<Both value={null} />, { wrapper: wrap(client) })
    const notice = await screen.findByTestId('composer-backend-config-error')
    expect(notice.textContent).toContain("Couldn't load the configured default backend")
    expect(screen.getByTestId('select')).toBeTruthy()
  })

  it.each([403, 404])('stays silent when the config read answers the permanent %s', async (status) => {
    vi.spyOn(api, 'acpBackends').mockResolvedValue({ backends: [row(''), row('kas')] })
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new ApiError(status, 'no'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<Both value={null} />, { wrapper: wrap(client) })
    await screen.findByTestId('select')
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByTestId('composer-backend-config-error')).toBeNull()
  })

  it('stores a probe body without a backend list as an empty list in the shared cache', async () => {
    // Other readers of ['acpBackends'] (the onboarding gate's poll) call
    // `data.backends.find` directly; a bare `{}` in the cache would throw there.
    vi.spyOn(api, 'acpBackends').mockResolvedValue({} as never)
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('no config'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<Both value={null} />, { wrapper: wrap(client) })
    await waitFor(() => expect(client.getQueryData(['acpBackends'])).toBeDefined())
    expect(client.getQueryData<{ backends: unknown[] }>(['acpBackends'])?.backends).toEqual([])
  })

  it('shows a refused switch as an error notice outside the picker control', async () => {
    vi.spyOn(api, 'acpBackends').mockResolvedValue({ backends: [row(''), row('kas')] })
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('no config'))
    const onDismiss = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(
      <Both value={null} switchError="a turn is in flight" onDismissSwitchError={onDismiss} />,
      { wrapper: wrap(client) },
    )
    const notice = await screen.findByTestId('composer-backend-switch-error')
    expect(notice.textContent).toContain('a turn is in flight')
    expect(notice.querySelector('[role="alert"]')).toBeTruthy()
    expect((await screen.findByTestId('composer-backend-picker')).contains(notice)).toBe(false)
    rerender(<Both value={null} switchError={null} />)
    expect(screen.queryByTestId('composer-backend-switch-error')).toBeNull()
  })

  it.each([403, 404])('stays silent on the permanent %s answer', async (status) => {
    vi.spyOn(api, 'acpBackends').mockRejectedValue(new ApiError(status, 'no'))
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('no config'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<Both value={null} />, { wrapper: wrap(client) })
    await waitFor(() => expect(api.acpBackends).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByTestId('composer-backend-picker-error')).toBeNull()
  })

  it('still shows a pick the list no longer offers', async () => {
    renderPicker({ value: 'gone' }, [row('')])
    await screen.findByTestId('select')
    expect(opts()).toContain('gone')
    expect(screen.getByTestId('select').getAttribute('data-value')).toBe('gone')
  })
})

describe('useAvailableModelsQuery with a backend pick', () => {
  it('fetches the picked backend on first mount under its own cache entry', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useAvailableModelsQuery({ backend: 'kas' }), { wrapper: wrap(client) })
    await waitFor(() => expect(result.current.data.map(m => m.name)).toContain('kas-model'))
    expect(fetchAvailableModels).toHaveBeenCalledWith('kas')
    expect(client.getQueryData(['available-models', 'acp', 'kas'])).toBeDefined()
    expect(client.getQueryData(['available-models', 'acp'])).toBeUndefined()
  })

  it('keeps the configured backend request unchanged without a pick', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useAvailableModelsQuery({ backend: null }), { wrapper: wrap(client) })
    await waitFor(() => expect(result.current.data.map(m => m.name)).toContain('kiro-model'))
    expect(fetchAvailableModels).toHaveBeenCalledWith(undefined)
    expect(client.getQueryData(['available-models', 'acp'])).toBeDefined()
  })
})
