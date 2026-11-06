/**
 * Settings > Agent Harness says which engine each Kiro harness runs: Kiro CLI runs
 * kiro-cli's V2 engine, Kiro Agent (KAS) runs V3. When the installed kiro-cli no
 * longer offers V2, a Kiro CLI pick is told so, with one click to Kiro Agent.
 * First-run setup's picker shows no engine line.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

const { patchConfigMock, kirocrewConfigMock, schemaMock, acpBackendsMock, kiroPrerequisiteMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(),
  schemaMock: vi.fn(),
  acpBackendsMock: vi.fn(),
  kiroPrerequisiteMock: vi.fn(),
}))

vi.mock('../api/client', async () => ({
  ApiError: (await import('../api/apiError')).ApiError,
  api: {
    kirocrewConfig: kirocrewConfigMock,
    patchConfig: patchConfigMock,
    acpBackends: acpBackendsMock,
    acpBackendRecheck: vi.fn(),
    kiroPrerequisite: kiroPrerequisiteMock,
    repairKiroPrerequisiteSpecs: vi.fn(),
  },
}))

vi.mock('../components/settingRef/useConfigSchema', () => ({
  useConfigSchema: () => schemaMock(),
}))

vi.mock('../pages/developer/KiroSignInCard', () => ({
  KiroSignInCard: () => <div data-testid="kiro-sign-in-card" />,
}))

vi.mock('../providers/adapters/acp', () => ({ clearCachedModels: vi.fn() }))

import KiroPrerequisiteGate from '../components/KiroPrerequisiteGate'
import { schemaWith, probeRow, wrap } from './AgentBackendTabTestUtils'
import { renderWithProviders } from './helpers'
import { probe, status } from './KiroPrerequisiteGateTestUtils'

const rows = [probeRow('kiro'), probeRow('kas'), probeRow('claude')]
const KIRO_HINT = 'Runs the Kiro CLI V2 engine.'
const KAS_HINT = 'Runs the V3 engine.'

const rowOf = (name: string) =>
  screen.getByRole('radio', { name }).closest('label') as HTMLElement

beforeEach(() => {
  localStorage.clear()
  patchConfigMock.mockClear()
  acpBackendsMock.mockReset()
  kirocrewConfigMock.mockReset()
  kiroPrerequisiteMock.mockReset()
  schemaMock.mockReturnValue(schemaWith(['kiro', 'claude', 'kas']))
  acpBackendsMock.mockResolvedValue({ backends: rows })
  kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'kiro' } })
})

describe('engine lines in Settings > Agent Harness', () => {
  it('names the engine beside each Kiro harness and nowhere else', async () => {
    wrap()
    await screen.findByRole('radio', { name: 'Kiro CLI' })
    expect(rowOf('Kiro CLI').textContent).toContain(KIRO_HINT)
    expect(rowOf('Kiro Agent').textContent).toContain(KAS_HINT)
    expect(rowOf('Kiro Agent').textContent).not.toContain(KIRO_HINT)
    expect(rowOf('Claude Code').textContent).not.toMatch(/engine/)
  })
})

describe('a Kiro CLI pick on a kiro-cli without V2', () => {
  it('says V2 is gone and switches to Kiro Agent in one click', async () => {
    acpBackendsMock.mockResolvedValue({ backends: rows, kiro_cli_v2_missing: true })
    wrap()
    const notice = await screen.findByTestId('kiro-cli-v2-missing')
    expect(notice.textContent).toContain('no longer offers its V2 engine')
    // The Kiro CLI row agrees with the notice instead of saying "ready to use".
    expect(screen.getByTestId('kiro-cli-v2-missing-row').textContent).toContain("Can't start")
    expect(screen.queryByText('Kiro CLI is installed and ready to use.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Use Kiro Agent' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'kas'))
  })

  it('stays quiet while V2 is there', async () => {
    acpBackendsMock.mockResolvedValue({ backends: rows, kiro_cli_v2_missing: false })
    wrap()
    await screen.findByRole('radio', { name: 'Kiro CLI' })
    expect(screen.queryByTestId('kiro-cli-v2-missing')).toBeNull()
  })

  it('is not shown when the pick is already Kiro Agent', async () => {
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    acpBackendsMock.mockResolvedValue({ backends: rows, kiro_cli_v2_missing: true })
    wrap()
    await screen.findByRole('radio', { name: 'Kiro CLI' })
    expect(screen.queryByTestId('kiro-cli-v2-missing')).toBeNull()
  })
})

describe('first-run setup', () => {
  it('shows no engine line in its picker', async () => {
    kiroPrerequisiteMock.mockResolvedValue(status())
    acpBackendsMock.mockResolvedValue({ backends: [probe()] })
    renderWithProviders(<KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>)
    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    await screen.findByRole('radio', { name: 'Claude Code' })
    expect(screen.queryByText(KIRO_HINT)).toBeNull()
    expect(screen.queryByText(KAS_HINT)).toBeNull()
  })
})
