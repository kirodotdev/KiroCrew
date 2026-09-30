// The decision-model picker: hosted Jev or a local model on this machine.
//
// Pinned here: the numbers each local option states (share of Jev's accuracy,
// memory, speed), the recommendation derived from the machine's total memory, and
// the write -- a preset id and a port, never an address.
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { DecisionsLocalModel, DecisionsProviderData } from '../../api/client/decisions'
import { DecisionsProviderPicker, recommendedPreset } from './DecisionsProviderPicker'

const PLUMB: DecisionsLocalModel = {
  id: 'plumb-4b',
  name: 'Plumb-4B',
  model: 'plumb-4b',
  default_port: 8102,
  jev_relative_pct: 103,
  hard_relative_pct: 109,
  peak_ram_gb: 14.8,
  recommended_total_ram_gb: 24,
  p50_secs: 2.4,
  p95_secs: 32,
  timeout_ms: 10000,
  setup_doc: 'https://example.invalid/local-decision-models.md',
  serve_command: 'python plumb_serve_cpu.py --port {port}',
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
  recommended_total_ram_gb: 12,
  p50_secs: 0.17,
  p95_secs: 0.51,
  serve_command: 'LAYA_PORT={port} laya-serve',
}

function providerOf(active: string): DecisionsProviderData {
  return {
    presets: [PLUMB, LAYA],
    active,
    configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
    configured_timeout_ms: 1000,
  }
}

function renderPicker({ active = 'jev', memGb = 32 as number | null, frozen = false } = {}) {
  // `null` stands for "the gateway reported no memory figure": an `undefined` here
  // would take the default instead.
  vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(providerOf(active))
  vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: memGb ?? undefined } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DecisionsProviderPicker frozen={frozen} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('recommendedPreset', () => {
  it('picks the first preset whose memory threshold the machine meets', () => {
    expect(recommendedPreset([PLUMB, LAYA], 32)).toBe('plumb-4b')
    expect(recommendedPreset([PLUMB, LAYA], 24)).toBe('plumb-4b')
    expect(recommendedPreset([PLUMB, LAYA], 16)).toBe('laya')
  })

  it('falls back to hosted Jev below every threshold or when memory is unknown', () => {
    expect(recommendedPreset([PLUMB, LAYA], 8)).toBe('jev')
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
    renderPicker({ active: 'jev', memGb: 16 })
    const laya = (await screen.findByText('Laya')).closest('label') as HTMLElement
    expect(laya.textContent).toMatch(/Recommended for this machine \(16\s*GB\)/)
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

  it('shows the start command with the chosen port filled in', async () => {
    renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /Laya/ }))
    expect(screen.getByText('LAYA_PORT=8104 laya-serve')).toBeTruthy()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '9100' } })
    expect(screen.getByText('LAYA_PORT=9100 laya-serve')).toBeTruthy()
  })

  it('writes a preset id and a port, never an address', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('laya'))
    renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /Laya/ }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '9100' } })
    fireEvent.click(screen.getByRole('button', { name: 'Use this model' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('laya', 9100))
  })

  it('switches back to hosted Jev with no port', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(providerOf('jev'))
    renderPicker({ active: 'laya' })
    fireEvent.click(await screen.findByRole('radio', { name: /Jev, hosted by TypeSafe/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Use this model' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('jev', undefined))
  })

  it('refuses a port outside the range the gateway accepts', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider')
    renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /Laya/ }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '80' } })
    expect(screen.getByRole('alert').textContent).toMatch(/1024 to 65535/)
    expect((screen.getByRole('button', { name: 'Use this model' }) as HTMLButtonElement).disabled).toBe(true)
    expect(save).not.toHaveBeenCalled()
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
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    expect(await screen.findByText('Could not read which decision model is configured.')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Ask the agent/i })).toBeTruthy()
  })

  it('starts the preset in use from the port its configured address names', async () => {
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue({
      ...providerOf('laya'),
      configured_endpoint: 'http://127.0.0.1:9100/v1/systemone',
    })
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <DecisionsProviderPicker frozen={false} />
      </QueryClientProvider>,
    )
    expect(((await screen.findByRole('textbox')) as HTMLInputElement).value).toBe('9100')
    expect(screen.getByText('LAYA_PORT=9100 laya-serve')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Use this model' })).toBeNull()
  })
})
