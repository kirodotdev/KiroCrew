/**
 * KiroCliEngineCard — Settings > Agent Harness, Kiro CLI row: which kiro-cli engine
 * a Kiro CLI choice runs.
 *
 * What is pinned:
 * - the picker shows the stored `agent.kiro_cli_engine`, and anything but `v2`
 *   (unset included) shows Automatic;
 * - the V2 option is labelled as temporary;
 * - a pick writes `agent.kiro_cli_engine` through the ordinary config PATCH, and
 *   re-picking the stored value writes nothing;
 * - a saved pick is reported as pending a restart (the setting is boot-only);
 * - a failed save is reported inline.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import {
  KIRO_CLI_ENGINE_CONFIG_KEY,
  KiroCliEngineCard,
  readKiroCliEngine,
} from '../pages/developer/KiroCliEngineCard'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn(),
      patchConfig: vi.fn().mockResolvedValue({}),
    },
  }
})

vi.mock('../components/settingRef/RestartRequiredBadge', () => ({
  SchemaRestartBadge: ({ configKey }: { configKey: string }) => (
    <span data-testid="restart-badge" data-key={configKey} />
  ),
}))

const kirocrewConfig = vi.mocked(api.kirocrewConfig)
const patchConfig = vi.mocked(api.patchConfig)

function stored(engine?: unknown) {
  kirocrewConfig.mockResolvedValue({ agent: engine === undefined ? {} : { kiro_cli_engine: engine } } as never)
}

async function picker(): Promise<HTMLElement> {
  return screen.findByRole('combobox', { name: 'Engine' })
}

async function pick(label: RegExp) {
  fireEvent.click(await picker())
  fireEvent.click(screen.getByRole('option', { name: label }))
}

beforeEach(() => {
  kirocrewConfig.mockReset()
  patchConfig.mockReset()
  patchConfig.mockResolvedValue({} as never)
})

describe('readKiroCliEngine', () => {
  it('reads v2 only for that exact value', () => {
    expect(readKiroCliEngine({ agent: { kiro_cli_engine: 'v2' } })).toBe('v2')
    for (const raw of [undefined, 'auto', 'v3', 'V2', 2, null]) {
      expect(readKiroCliEngine({ agent: { kiro_cli_engine: raw } })).toBe('auto')
    }
    expect(readKiroCliEngine(undefined)).toBe('auto')
  })
})

describe('KiroCliEngineCard', () => {
  it('shows Automatic when nothing is stored, and V2 as temporary', async () => {
    stored()
    renderWithProviders(<KiroCliEngineCard />)
    const select = await picker()
    expect(select).toHaveTextContent('Automatic')
    fireEvent.click(select)
    const labels = screen.getAllByRole('option').map(o => o.textContent)
    expect(labels).toEqual(['Automatic', 'V2 (until kiro-cli removes it)'])
    expect(screen.getByTestId('restart-badge').getAttribute('data-key')).toBe(KIRO_CLI_ENGINE_CONFIG_KEY)
  })

  it('shows the stored v2', async () => {
    stored('v2')
    renderWithProviders(<KiroCliEngineCard />)
    expect(await picker()).toHaveTextContent('V2 (until kiro-cli removes it)')
  })

  it('writes the pick through the config PATCH and says a restart is pending', async () => {
    stored()
    renderWithProviders(<KiroCliEngineCard />)
    expect(screen.queryByTestId('kiro-cli-engine-restart-pending')).toBeNull()
    await pick(/V2/)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith(KIRO_CLI_ENGINE_CONFIG_KEY, 'v2'))
    expect(await screen.findByTestId('kiro-cli-engine-restart-pending')).toBeTruthy()
  })

  it('writes nothing when the stored value is picked again', async () => {
    stored('v2')
    renderWithProviders(<KiroCliEngineCard />)
    await pick(/V2/)
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('reports a failed save', async () => {
    stored()
    patchConfig.mockRejectedValue(new Error('boom'))
    renderWithProviders(<KiroCliEngineCard />)
    await pick(/V2/)
    expect(await screen.findByText('Could not save the engine.')).toBeTruthy()
    expect(screen.queryByTestId('kiro-cli-engine-restart-pending')).toBeNull()
  })
})
