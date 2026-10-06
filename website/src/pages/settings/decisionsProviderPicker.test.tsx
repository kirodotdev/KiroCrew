// The decision-model picker: hosted Jev or a local model on this machine.
//
// Pinned here: the numbers each local option states (share of Jev's accuracy,
// memory, speed), the recommendation derived from the machine's total memory, and
// the write -- a preset id, never an address or a port -- and how the card follows
// the gateway while it downloads, installs and runs a local model.
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { defaultScheduler, notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { namedCeiling } from '../../test/namedCeiling'

import { api } from '../../api/client'
import type {
  DecisionsLocalModel,
  DecisionsLocalRuntimeData,
  DecisionsProviderData,
  DecisionsRuntimeStatus,
} from '../../api/client/decisions'
import {
  DECISIONS_PROVIDER_QUERY_KEY,
  DecisionsProviderPicker,
  recommendedPreset,
  RUNTIME_POLL_MS,
} from './DecisionsProviderPicker'

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

async function renderPicker({
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
  const view = render(
    <QueryClientProvider client={client}>
      <DecisionsProviderPicker frozen={frozen} consentOn={consentOn} />
    </QueryClientProvider>,
  )
  // The machine's memory is a SEPARATE read from the preset list, so a frame that
  // shows the presets says nothing about the recommendation badge, which needs
  // both. Both reads start on mount; wait until neither is still in flight.
  await waitFor(() => expect(client.isFetching()).toBe(0))
  return view
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

/**
 * Ceiling for the status poll's first read. It sits behind a chain: the provider
 * read resolves, the card renders it as busy, and that render turns the status
 * query's `enabled` on, so the read goes out in the commit after it.
 */
const STATUS_POLL_STARTS = namedCeiling('STATUS_POLL_STARTS', 5000)

/**
 * Mount a card whose model is being prepared, with the status poll on a FAKE
 * clock. React Query arms the poll's interval when a status read settles, on
 * whatever setInterval is global at that moment, so the first read is held open
 * until fake timers are on: one armed on the real clock would fire on its own.
 * After this returns, use `settle` and `stepPoll`, never waitFor or findBy:
 * Testing Library's waits do not advance vitest's fake clock.
 */
async function mountPreparing(
  provider: DecisionsProviderData,
  later: () => Promise<DecisionsLocalRuntimeData>,
) {
  const first = deferred<DecisionsLocalRuntimeData>()
  const providerRead = vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(provider)
  const status = vi
    .spyOn(api, 'getDecisionsLocalRuntime')
    .mockImplementationOnce(() => first.promise)
    .mockImplementation(later)
  vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <DecisionsProviderPicker frozen={false} />
    </QueryClientProvider>,
  )
  // The poll starts in an effect after the frame that shows the provider read.
  await waitFor(() => expect(status).toHaveBeenCalledTimes(1), STATUS_POLL_STARTS)
  vi.useFakeTimers()
  // React Query hands results to React on a setTimeout(0); on the fake clock one
  // queued mid-step lands a millisecond past the step, so run them as microtasks.
  notifyManager.setScheduler(queueMicrotask)
  return { providerRead, status, first }
}

/** Run `action` and let everything it settles render. */
const settle = (action: () => void) =>
  act(async () => { action(); await vi.advanceTimersByTimeAsync(0) })
/** Move the fake clock on by whole poll periods. */
const stepPoll = (periods = 1) =>
  act(async () => { await vi.advanceTimersByTimeAsync(RUNTIME_POLL_MS * periods) })

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  notifyManager.setScheduler(defaultScheduler)
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
    await renderPicker()
    expect(await screen.findByText(/About 103% of Jev's accuracy, 109% on hard decisions/)).toBeTruthy()
    expect(screen.getByText(/About 67% of Jev's accuracy, 47% on hard decisions/)).toBeTruthy()
    expect(screen.getByText(/recommended with 24\s*GB or more/)).toBeTruthy()
  })

  it('marks the model this machine is suited to, and the one in use', async () => {
    await renderPicker({ active: 'jev', memGb: 32 })
    const laya = (await screen.findByText('Laya')).closest('label') as HTMLElement
    expect(laya.textContent).toMatch(/Recommended for this machine's 32\s*GB of memory/)
    const plumb = screen.getByText('Plumb-4B').closest('label') as HTMLElement
    expect(plumb.textContent).not.toMatch(/Recommended/)
    const jev = screen.getByText('Jev, hosted by TypeSafe').closest('label') as HTMLElement
    expect(jev.textContent).toMatch(/In use/)
  })

  it('recommends nothing when the machine memory is unknown', async () => {
    await renderPicker({ memGb: null })
    await screen.findByText('Plumb-4B')
    expect(screen.queryByText(/Recommended for this machine/)).toBeNull()
  })

  it('says what the first use downloads before the reader commits', async () => {
    await renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /Plumb-4B/ }))
    expect(screen.getByText(/downloads 8\.4\s*GB of model files and about 1 GB of software/)).toBeTruthy()
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('warns what the model costs the rest of the machine, and only for a local model', async () => {
    await renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /Plumb-4B/ }))
    expect(
      screen.getByText(/Plumb-4B keeps about 15\s*GB of memory in use.*fewer subagents.*Pick No model to free it/),
    ).toBeTruthy()
    fireEvent.click(screen.getByRole('radio', { name: /Jev, hosted/ }))
    expect(screen.queryByText(/of memory in use/)).toBeNull()
  })

  it('says a downloaded model only needs starting', async () => {
    await renderPicker({
      data: providerOf('jev', {}, [PLUMB, { ...LAYA, installed: true }]),
    })
    fireEvent.click(await screen.findByRole('radio', { name: /Laya/ }))
    expect(screen.getByText(/Downloaded\./)).toBeTruthy()
  })

  it('writes a preset id, never an address or a port', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('laya'))
    await renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /Laya/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Use this model' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('laya'))
  })

  it('switches back to hosted Jev', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('jev'))
    await renderPicker({ active: 'laya' })
    fireEvent.click(await screen.findByRole('radio', { name: /Jev, hosted by TypeSafe/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Use this model' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('jev'))
  })

  it('stops the local model by choosing no model', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('none'))
    await renderPicker({ active: 'laya' })
    fireEvent.click(await screen.findByRole('radio', { name: 'No model' }))
    fireEvent.click(screen.getByRole('button', { name: 'Use this model' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('none'))
  })

  it('shows download progress for the model being prepared', async () => {
    await renderPicker({
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
    await renderPicker({
      data: providerOf('plumb-4b', { preset: 'plumb-4b', state: 'downloading', bytes_done: 1e9, bytes_total: 8.4e9 }),
    })
    expect(await screen.findByText(/Decisions are skipped until it is ready\. To stop, pick another model/)).toBeTruthy()
    const plumb = screen.getByText('Plumb-4B').closest('label') as HTMLElement
    expect(plumb.textContent).not.toMatch(/In use/)
  })

  it('claims no "In use" while the Decisions switch is off, and says so under a running model', async () => {
    await renderPicker({ consentOn: false, data: providerOf('laya', { preset: 'laya', state: 'running', port: 8104 }) })
    expect(await screen.findByText(/nothing is sent to it while the Decisions switch above is off/)).toBeTruthy()
    const laya = screen.getByText('Laya').closest('label') as HTMLElement
    expect(laya.textContent).not.toMatch(/In use/)
    expect(screen.queryByText('Running on this machine.')).toBeNull()
  })

  it('never recommends hosted Jev for the machine memory', async () => {
    await renderPicker({ memGb: 4 })
    const jev = (await screen.findByText(/Jev, hosted by TypeSafe/)).closest('label') as HTMLElement
    expect(jev.textContent).not.toMatch(/Recommended/)
  })

  it('marks a local model "In use" once its server runs', async () => {
    await renderPicker({ data: providerOf('laya', { preset: 'laya', state: 'running', port: 8104 }) })
    const laya = (await screen.findByText('Laya')).closest('label') as HTMLElement
    expect(laya.textContent).toMatch(/In use/)
  })

  it('does not recommend a model that just failed to start here', async () => {
    await renderPicker({ memGb: 32, data: providerOf('plumb-4b', { preset: 'plumb-4b', state: 'error', error: 'OOM' }) })
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
    const { status, first } = await mountPreparing(
      providerOf('laya', { preset: 'laya', state: 'downloading', bytes_done: 1e8, bytes_total: 8e8 }),
      () => Promise.resolve({
        runtime: { ...IDLE, preset: 'laya', state: 'downloading', bytes_done: 4e8, bytes_total: 8e8 },
        installed: [],
      }),
    )
    await settle(() => first.reject(Object.assign(new Error('502'), { status: 502 })))
    expect(screen.getByText(/this progress may be out of date/)).toBeTruthy()
    await stepPoll()
    expect(status).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(/this progress may be out of date/)).toBeNull()
    // The second read is what rendered: the provider read said 0.1 GB.
    expect(screen.getByText(/Downloading the model: 0\.4\s*GB of 0\.8\s*GB/)).toBeTruthy()
  })

  it('polls the audit-free status route while the model is being prepared, then stops', async () => {
    const { providerRead, status, first } = await mountPreparing(
      providerOf('laya', { preset: 'laya', state: 'starting', port: 8104 }),
      () => Promise.resolve({ runtime: { ...IDLE, preset: 'laya', state: 'running', port: 8104 }, installed: ['laya'] }),
    )
    expect(screen.getByText(/Starting the model/)).toBeTruthy()
    await settle(() => first.resolve({ runtime: { ...IDLE, preset: 'laya', state: 'starting', port: 8104 }, installed: [] }))
    await stepPoll()
    expect(status).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Running on this machine.')).toBeTruthy()
    // The provider route, which audits and evaluates governance, is read once only.
    expect(providerRead).toHaveBeenCalledTimes(1)
    // Running is settled, so the poll stops however long the card stays open.
    await stepPoll(3)
    expect(status).toHaveBeenCalledTimes(2)
  })

  it('keeps the provider read when the status route answers something malformed', async () => {
    const { first } = await mountPreparing(
      providerOf('plumb-4b', { preset: 'plumb-4b', state: 'downloading', bytes_done: 1e9, bytes_total: 8.4e9 }),
      () => Promise.resolve([] as never),
    )
    // The malformed answer lands and renders inside `settle`, before the card is read.
    await settle(() => first.resolve([] as never))
    expect(screen.getByText(/Downloading the model: 1\s*GB of 8\.4\s*GB/)).toBeTruthy()
  })

  it('reports a model that could not start, with its log and a retry', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('laya'))
    await renderPicker({
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
    await renderPicker({
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
    await renderPicker({ active: 'laya' })
    await screen.findByText('Laya')
    expect(screen.queryByRole('button', { name: 'Use this model' })).toBeNull()
  })

  it('holds every control while the card is frozen', async () => {
    await renderPicker({ frozen: true })
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
    // On the answer, not the request: before any answer the picker draws nothing too.
    await waitFor(() => expect(client.getQueryState(DECISIONS_PROVIDER_QUERY_KEY)?.status).toBe('error'))
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
