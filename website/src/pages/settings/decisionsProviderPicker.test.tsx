// The decision-model picker: hosted Jev or a local model on this machine.
//
// Pinned here: the numbers each local option states (share of Jev's accuracy,
// memory, speed), the recommendation derived from the machine's total memory, and
// the write -- a preset id, never an address or a port -- and how the card follows
// the gateway while it downloads, installs and runs a local model.
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { DecisionsLocalModel, DecisionsProviderData, DecisionsRuntimeStatus } from '../../api/client/decisions'
import { DecisionsProviderPicker, recommendedPreset } from './DecisionsProviderPicker'

const PLUMB: DecisionsLocalModel = {
  id: 'plumb-4b',
  name: 'Plumb-4B',
  model: 'plumb-4b',
  default_port: 8102,
  jev_relative_pct: 103,
  hard_relative_pct: 109,
  peak_ram_gb: 14.8,
  recommended_total_ram_gb: 60,
  p50_secs: 2.4,
  p95_secs: 32,
  timeout_ms: 5000,
  download_bytes: 8_431_584_407,
  installed: false,
}
const LAYA: DecisionsLocalModel = {
  ...PLUMB,
  id: 'laya',
  name: 'Laya',
  model: 'english',
  default_port: 8104,
  jev_relative_pct: 67,
  hard_relative_pct: 47,
  peak_ram_gb: 6,
  recommended_total_ram_gb: 24,
  p50_secs: 0.17,
  p95_secs: 0.51,
  download_bytes: 846_207_419,
}
const STRANDS: DecisionsLocalModel = {
  ...PLUMB,
  id: 'strands-decider-2b',
  name: 'Strands Decider 2B',
  model: 'strands-decider-2b',
  default_port: 8106,
  jev_relative_pct: 84,
  hard_relative_pct: 69,
  peak_ram_gb: 12,
  recommended_total_ram_gb: 48,
  p50_secs: 0.64,
  p95_secs: 11.7,
  download_bytes: 4_639_855_040,
}

const IDLE: DecisionsRuntimeStatus = {
  preset: '',
  state: 'idle',
  port: 0,
  bytes_done: 0,
  bytes_total: 0,
  error: '',
}

function providerOf(
  active: string,
  runtime: Partial<DecisionsRuntimeStatus> = {},
  presets = [PLUMB, LAYA],
): DecisionsProviderData {
  return {
    presets,
    active,
    configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
    runtime: { ...IDLE, ...runtime },
  }
}

function renderPicker({
  active = 'jev',
  memGb = 32 as number | null,
  frozen = false,
  data = undefined as DecisionsProviderData | undefined,
  consentOn = true,
} = {}) {
  // `null` stands for "the gateway reported no memory figure": an `undefined` here
  // would take the default instead.
  vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(data ?? providerOf(active))
  vi.spyOn(api, 'system').mockResolvedValue({
    mem_total_gb: memGb ?? undefined,
  } as never)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <DecisionsProviderPicker frozen={frozen} consentOn={consentOn} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('recommendedPreset', () => {
  it('picks the first preset whose memory threshold the machine meets', () => {
    expect(recommendedPreset([PLUMB, LAYA], 64)).toBe('plumb-4b')
    expect(recommendedPreset([PLUMB, LAYA], 60)).toBe('plumb-4b')
    // A 64 GB / 24 GB machine as the gateway reports it, in GiB after reserve.
    expect(recommendedPreset([PLUMB, LAYA], 59.4)).toBe('plumb-4b')
    expect(recommendedPreset([PLUMB, LAYA], 23.4)).toBe('laya')
    expect(recommendedPreset([PLUMB, LAYA], 32)).toBe('laya')
  })

  it('steps down through three presets as memory shrinks, each at four times its peak', () => {
    const all = [PLUMB, STRANDS, LAYA]
    expect(recommendedPreset(all, 64)).toBe('plumb-4b')
    expect(recommendedPreset(all, 59.4)).toBe('plumb-4b')
    expect(recommendedPreset(all, 48)).toBe('strands-decider-2b')
    expect(recommendedPreset(all, 47.0)).toBe('strands-decider-2b')
    expect(recommendedPreset(all, 32)).toBe('laya')
    expect(recommendedPreset(all, 16)).toBe('jev')
  })

  it('falls back to hosted Jev below every threshold or when memory is unknown', () => {
    expect(recommendedPreset([PLUMB, LAYA], 16)).toBe('jev')
    expect(recommendedPreset([PLUMB, LAYA], undefined)).toBe('jev')
    expect(recommendedPreset([PLUMB, LAYA], Number.NaN)).toBe('jev')
    expect(recommendedPreset([PLUMB, LAYA], 0)).toBe('jev')
  })
})

describe('DecisionsProviderPicker', () => {
  it('states each local model against Jev: accuracy, memory and speed', async () => {
    renderPicker()
    expect(await screen.findByText(/About 103% of Jev's accuracy, 109% on hard decisions/)).toBeTruthy()
    expect(screen.getByText(/About 67% of Jev's accuracy, 47% on hard decisions/)).toBeTruthy()
    expect(screen.getByText(/recommended with 24\s*GB or more/)).toBeTruthy()
  })

  it('marks the model this machine is suited to, and the one in use', async () => {
    renderPicker({ active: 'jev', memGb: 32 })
    const laya = (await screen.findByText('Laya')).closest('label') as HTMLElement
    expect(laya.textContent).toMatch(/Recommended for this machine's 32\s*GB of memory/)
    const plumb = screen.getByText('Plumb-4B').closest('label') as HTMLElement
    expect(plumb.textContent).not.toMatch(/Recommended/)
    const jev = screen.getByText('Jev, hosted by TypeSafe').closest('label') as HTMLElement
    expect(jev.textContent).toMatch(/In use/)
  })

  it('recommends nothing when the machine memory is unknown', async () => {
    renderPicker({ memGb: null })
    await screen.findByText('Plumb-4B')
    expect(screen.queryByText(/Recommended for this machine/)).toBeNull()
  })

  it('says what the first use downloads before the reader commits', async () => {
    renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /Plumb-4B/ }))
    expect(screen.getByText(/downloads 8\.4\s*GB of model files and about 1 GB of software/)).toBeTruthy()
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('warns what the model costs the rest of the machine, and only for a local model', async () => {
    renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /Plumb-4B/ }))
    expect(
      screen.getByText(/Plumb-4B keeps about 15\s*GB of memory in use.*fewer subagents.*Pick No model to free it/),
    ).toBeTruthy()
    fireEvent.click(screen.getByRole('radio', { name: /Jev, hosted/ }))
    expect(screen.queryByText(/of memory in use/)).toBeNull()
  })

  it('says a downloaded model only needs starting', async () => {
    renderPicker({
      data: providerOf('jev', {}, [PLUMB, { ...LAYA, installed: true }]),
    })
    fireEvent.click(await screen.findByRole('radio', { name: /Laya/ }))
    expect(screen.getByText(/Downloaded\./)).toBeTruthy()
  })

  it('writes a preset id, never an address or a port', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('laya'))
    renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /Laya/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Use this model' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('laya'))
  })

  it('switches back to hosted Jev', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('jev'))
    renderPicker({ active: 'laya' })
    fireEvent.click(await screen.findByRole('radio', { name: /Jev, hosted by TypeSafe/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Use this model' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('jev'))
  })

  it('stops the local model by choosing no model', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('none'))
    renderPicker({ active: 'laya' })
    fireEvent.click(await screen.findByRole('radio', { name: 'No model' }))
    fireEvent.click(screen.getByRole('button', { name: 'Use this model' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('none'))
  })

  it('shows download progress for the model being prepared', async () => {
    renderPicker({
      data: providerOf('plumb-4b', {
        preset: 'plumb-4b',
        state: 'downloading',
        bytes_done: 2.1e9,
        bytes_total: 8.4e9,
      }),
    })
    expect(await screen.findByText(/Downloading the model: 2\.1\s*GB of 8\.4\s*GB/)).toBeTruthy()
    const bar = screen.getByRole('progressbar', {
      name: 'Model download progress',
    }) as HTMLProgressElement
    expect(bar.value).toBe(2.1e9)
  })

  it('says decisions are skipped while preparing, how to stop, and claims no "In use" yet', async () => {
    renderPicker({
      data: providerOf('plumb-4b', { preset: 'plumb-4b', state: 'downloading', bytes_done: 1e9, bytes_total: 8.4e9 }),
    })
    expect(await screen.findByText(/Decisions are skipped until it is ready\. To stop, pick another model/)).toBeTruthy()
    const plumb = screen.getByText('Plumb-4B').closest('label') as HTMLElement
    expect(plumb.textContent).not.toMatch(/In use/)
  })

  it('claims no "In use" while the Decisions switch is off, and says so under a running model', async () => {
    renderPicker({ consentOn: false, data: providerOf('laya', { preset: 'laya', state: 'running', port: 8104 }) })
    expect(await screen.findByText(/nothing is sent to it while the Decisions switch above is off/)).toBeTruthy()
    const laya = screen.getByText('Laya').closest('label') as HTMLElement
    expect(laya.textContent).not.toMatch(/In use/)
    expect(screen.queryByText('Running on this machine.')).toBeNull()
  })

  it('never recommends hosted Jev for the machine memory', async () => {
    renderPicker({ memGb: 4 })
    const jev = (await screen.findByText(/Jev, hosted by TypeSafe/)).closest('label') as HTMLElement
    expect(jev.textContent).not.toMatch(/Recommended/)
  })

  it('marks a local model "In use" once its server runs', async () => {
    renderPicker({ data: providerOf('laya', { preset: 'laya', state: 'running', port: 8104 }) })
    const laya = (await screen.findByText('Laya')).closest('label') as HTMLElement
    expect(laya.textContent).toMatch(/In use/)
  })

  it('does not recommend a model that just failed to start here', async () => {
    renderPicker({ memGb: 32, data: providerOf('plumb-4b', { preset: 'plumb-4b', state: 'error', error: 'OOM' }) })
    const plumb = (await screen.findByText('Plumb-4B')).closest('label') as HTMLElement
    expect(plumb.textContent).not.toMatch(/Recommended/)
  })

  it('says so when the progress poll fails, with the hand-off', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(
      providerOf('laya', { preset: 'laya', state: 'downloading', bytes_done: 1e8, bytes_total: 8e8 }),
    )
    vi.spyOn(api, 'getDecisionsLocalRuntime').mockRejectedValue(Object.assign(new Error('502'), { status: 502 }))
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/this progress may be out of date/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Ask the agent/i })).toBeTruthy()
  })

  it('keeps polling after a failed progress poll, so a download does not freeze', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(
      providerOf('laya', { preset: 'laya', state: 'downloading', bytes_done: 1e8, bytes_total: 8e8 }),
    )
    const status = vi
      .spyOn(api, 'getDecisionsLocalRuntime')
      .mockRejectedValueOnce(Object.assign(new Error('502'), { status: 502 }))
      .mockResolvedValue({
        runtime: { ...IDLE, preset: 'laya', state: 'downloading', bytes_done: 4e8, bytes_total: 8e8 },
        installed: [],
      })
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/this progress may be out of date/)).toBeTruthy()
    await waitFor(() => expect(status.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 4000 })
    await waitFor(() => expect(screen.queryByText(/this progress may be out of date/)).toBeNull(), { timeout: 4000 })
  })

  it('polls the audit-free status route while the model is being prepared, then stops', async () => {
    const provider = vi
      .spyOn(api, 'getDecisionsProvider')
      .mockResolvedValue(providerOf('laya', { preset: 'laya', state: 'starting', port: 8104 }))
    const status = vi
      .spyOn(api, 'getDecisionsLocalRuntime')
      .mockResolvedValueOnce({ runtime: { ...IDLE, preset: 'laya', state: 'starting', port: 8104 }, installed: [] })
      .mockResolvedValue({ runtime: { ...IDLE, preset: 'laya', state: 'running', port: 8104 }, installed: ['laya'] })
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/Starting the model/)).toBeTruthy()
    expect(await screen.findByText('Running on this machine.', {}, { timeout: 4000 })).toBeTruthy()
    // The provider route, which audits and evaluates governance, is read once only.
    expect(provider).toHaveBeenCalledTimes(1)
    const settled = status.mock.calls.length
    await new Promise(r => setTimeout(r, 2500))
    expect(status.mock.calls.length).toBe(settled)
  })

  it('keeps the provider read when the status route answers something malformed', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(
      providerOf('plumb-4b', { preset: 'plumb-4b', state: 'downloading', bytes_done: 1e9, bytes_total: 8.4e9 }),
    )
    vi.spyOn(api, 'getDecisionsLocalRuntime').mockResolvedValue([] as never)
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(api.getDecisionsLocalRuntime).toHaveBeenCalled())
    // Let the malformed answer land and render before asserting the card survived it.
    const settled = vi.mocked(api.getDecisionsLocalRuntime).mock.results[0].value as Promise<unknown>
    await settled
    await new Promise(r => setTimeout(r, 50))
    expect(screen.getByText(/Downloading the model: 1\s*GB of 8\.4\s*GB/)).toBeTruthy()
  })

  it('reports a model that could not start, with its log and a retry', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('laya'))
    renderPicker({
      data: providerOf('laya', {
        preset: 'laya',
        state: 'error',
        port: 8104,
        error: 'MemoryError: out of memory',
      }),
    })
    expect(await screen.findByText('The model could not be started.')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Ask the agent/i })).toBeTruthy()
    expect(screen.getByText('MemoryError: out of memory')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('laya'))
  })

  it('offers to remove a download that is not in use, and only that one', async () => {
    const remove = vi.spyOn(api, 'removeDecisionsLocalModel').mockResolvedValue(providerOf('laya'))
    renderPicker({
      data: providerOf('laya', { preset: 'laya', state: 'running', port: 8104 }, [
        { ...PLUMB, installed: true },
        { ...LAYA, installed: true },
      ]),
    })
    const button = await screen.findByRole('button', {
      name: /Remove Plumb-4B download \(8\.4\s*GB\)/,
    })
    expect(screen.queryByRole('button', { name: /Remove Laya download/ })).toBeNull()
    fireEvent.click(button)
    await waitFor(() => expect(remove).toHaveBeenCalledWith('plumb-4b'))
  })

  it('offers no save while nothing differs from what is configured', async () => {
    renderPicker({ active: 'laya' })
    await screen.findByText('Laya')
    expect(screen.queryByRole('button', { name: 'Use this model' })).toBeNull()
  })

  it('holds every control while the card is frozen', async () => {
    renderPicker({ frozen: true })
    const radios = await screen.findAllByRole('radio')
    expect(radios.every(r => (r as HTMLInputElement).disabled)).toBe(true)
  })

  it('draws nothing when the gateway has no provider route', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockRejectedValue(Object.assign(new Error('404'), { status: 404 }))
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const { container } = render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(api.getDecisionsProvider).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('says a failed read out loud, with the hand-off', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    expect(await screen.findByText('Could not read which decision model is configured.')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Ask the agent/i })).toBeTruthy()
  })

  it('says so when the machine memory cannot be read, and still offers the models', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(providerOf('jev'))
    vi.spyOn(api, 'system').mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    expect(
      await screen.findByText("Could not read this machine's memory, so no model is marked as recommended."),
    ).toBeTruthy()
    expect(screen.getByRole('button', { name: /Ask the agent/i })).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Plumb-4B' })).toBeTruthy()
  })
})

describe('fleet policy', () => {
  it('greys out hosted Jev when policy withdraws it and keeps local models choosable', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue({
      ...providerOf('plumb-4b'),
      hosted_permitted: false,
      local_permitted: true,
    })
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    const jev = await screen.findByRole('radio', {
      name: /Jev, hosted by TypeSafe/i,
    })
    expect(jev).toBeDisabled()
    expect(screen.getByRole('radio', { name: 'Laya' })).not.toBeDisabled()
    expect(screen.getAllByText(/Turned off by your organization's policy/i)).toHaveLength(1)
  })

  it('keeps "No model" choosable when policy withdraws both sides', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue({
      ...providerOf('laya'),
      hosted_permitted: false,
      local_permitted: false,
    })
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('radio', { name: 'No model' })).not.toBeDisabled()
    expect(screen.getByRole('radio', { name: 'Plumb-4B' })).toBeDisabled()
  })

  it('recommends no local model when policy withdraws local models', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue({
      ...providerOf('jev'),
      local_permitted: false,
    })
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('radio', { name: 'Laya' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: 'Plumb-4B' })).toBeDisabled()
    expect(screen.getAllByText(/Turned off by your organization's policy/i)).toHaveLength(2)
  })
})
