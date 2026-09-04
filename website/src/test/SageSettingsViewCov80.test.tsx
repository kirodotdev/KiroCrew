import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * Settings writes through on every change — there is no Save button to forget —
 * so what matters is that each control sends the RIGHT patch (and that clearing
 * the model sends null, i.e. "inherit the agent default", not an empty string).
 */
vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    settings: vi.fn(),
    putSettings: vi.fn(),
  },
}))

import { sageApi } from '../apps/code-review-sage/api'
import SettingsView from '../apps/code-review-sage/views/SettingsView'

const api = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

function mount() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  })
  return { qc, ...render(<QueryClientProvider client={qc}><SettingsView /></QueryClientProvider>) }
}

/** Radix Select: open the trigger, then click the option by its accessible name. */
async function pick(triggerName: RegExp, optionName: string | RegExp): Promise<void> {
  fireEvent.click(await screen.findByRole('combobox', { name: triggerName }))
  fireEvent.click(await screen.findByRole('option', { name: optionName }))
}

const SETTINGS = {
  settings: {
    model: 'zzz-model-a', effort: 'medium', active_namespaces: ['default'], max_concurrent: 2,
  },
  models: ['zzz-model-a', 'zzz-model-b'],
  efforts: ['zzz-low', 'zzz-high'],
  namespaces: ['default'],
  max_concurrent_max: 3,
}

beforeEach(() => {
  vi.clearAllMocks()
  api.settings.mockResolvedValue(SETTINGS)
  api.putSettings.mockResolvedValue(SETTINGS)
})

describe('SettingsView', () => {
  it('says it is loading before the settings arrive', async () => {
    let release: (v: unknown) => void = () => {}
    api.settings.mockReturnValue(new Promise(res => { release = res }))
    mount()
    expect(screen.getByText(/Loading settings/i)).toBeInTheDocument()
    release(SETTINGS)
    await waitFor(() => expect(screen.queryByText(/Loading settings/i)).not.toBeInTheDocument())
  })

  it('shows the fetch error instead of empty controls', async () => {
    api.settings.mockRejectedValue(new Error('zzz settings unreadable'))
    mount()
    expect(await screen.findByText('zzz settings unreadable')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: /Review model/i })).not.toBeInTheDocument()
  })

  it('renders one control per setting, seeded from the server', async () => {
    mount()
    expect(await screen.findByRole('combobox', { name: /Review model/i })).toHaveTextContent('zzz-model-a')
    expect(screen.getByRole('combobox', { name: /Reasoning effort/i })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /Reviews at once/i })).toHaveTextContent('2')
  })

  it('writes the model through immediately', async () => {
    mount()
    await pick(/Review model/i, 'zzz-model-b')
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ model: 'zzz-model-b' }))
  })

  it('clears the model to null rather than an empty string', async () => {
    mount()
    // The clear row: null is what the backend reads as "inherit the agent config".
    await pick(/Review model/i, /Default \(agent config\)/i)
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ model: null }))
  })

  it('writes effort and concurrency through, concurrency as a number', async () => {
    mount()
    await pick(/Reasoning effort/i, 'zzz-high')
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ effort: 'zzz-high' }))

    await pick(/Reviews at once/i, '3')
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ max_concurrent: 3 }))
  })

  it('caps the concurrency options at the server-declared maximum', async () => {
    mount()
    fireEvent.click(await screen.findByRole('combobox', { name: /Reviews at once/i }))
    const labels = (await screen.findAllByRole('option')).map(o => o.textContent)
    expect(labels).toEqual(['1', '2', '3'])
  })

  it('surfaces a failed write rather than pretending it saved', async () => {
    api.putSettings.mockRejectedValue(new Error('zzz write refused'))
    mount()
    await pick(/Reasoning effort/i, 'zzz-low')
    expect(await screen.findByText('zzz write refused')).toBeInTheDocument()
  })

  it('refreshes the runs list too, so the rail stops showing the previous model', async () => {
    const { qc } = mount()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    await pick(/Reasoning effort/i, 'zzz-low')
    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['code-review-sage', 'runs'] })
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['code-review-sage', 'settings'] })
  })
})
