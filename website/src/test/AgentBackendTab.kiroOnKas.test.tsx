/**
 * A stored Kiro CLI pick that sessions run on KAS gets a one-click save of KAS.
 *
 * kiro-cli serves `acp` on its v3 (KAS) engine, and the server resolves a stored
 * `"kiro"` to KAS while kiro-cli offers that engine, so `GET /api/acp-backends`
 * says `kiro_selection_runs_on_kas`. The panel then shows one line and one button
 * that PATCHes `agent.acp_backend` to `kas`, so the setting says what runs
 * (the a-kiro-cli-choice-runs-on-kas entry in docs/decisions).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

const { patchConfigMock, kirocrewConfigMock, schemaMock, acpBackendsMock, acpBackendRecheckMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({ agent: { acp_backend: 'kas' } })),
  schemaMock: vi.fn(),
  acpBackendsMock: vi.fn(),
  acpBackendRecheckMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: kirocrewConfigMock,
    patchConfig: patchConfigMock,
    acpBackends: acpBackendsMock,
    acpBackendRecheck: acpBackendRecheckMock,
  },
}))

vi.mock('../components/settingRef/useConfigSchema', () => ({
  useConfigSchema: () => schemaMock(),
}))

vi.mock('../pages/developer/KiroSignInCard', () => ({
  KiroSignInCard: () => <div data-testid="kiro-sign-in-card" />,
}))

import { schemaWith, probeRow, wrap } from './AgentBackendTabTestUtils'

const rows = [probeRow('kiro'), probeRow('kas'), probeRow('claude')]

beforeEach(() => {
  localStorage.clear()
  patchConfigMock.mockClear()
  acpBackendsMock.mockReset()
  acpBackendRecheckMock.mockReset()
  schemaMock.mockReturnValue(schemaWith(['kiro', 'claude', 'kas']))
})

describe('a Kiro CLI pick that runs on KAS', () => {
  it('offers one click that saves KAS as the setting', async () => {
    acpBackendsMock.mockResolvedValue({ backends: rows, kiro_selection_runs_on_kas: true })
    wrap()
    const notice = await screen.findByTestId('kiro-runs-on-kas')
    // Named by the option's visible name, so the reader can match it to the list.
    expect(notice.textContent).toContain('Kiro CLI now runs on Kiro Agent, so new sessions already use it.')
    fireEvent.click(screen.getByRole('button', { name: 'Save Kiro Agent as my setting' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'kas'))
    // The click visibly lands where the callout was.
    expect((await screen.findByTestId('kiro-runs-on-kas-saved')).textContent).toBe(
      'Saved: Kiro Agent.',
    )
    expect(screen.queryByTestId('kiro-runs-on-kas')).toBeNull()
    // The saved pick changes what the probe reports, so the panel asks again.
    await waitFor(() => expect(acpBackendsMock.mock.calls.length).toBeGreaterThan(1))
  })

  it('names both states and marks the Kiro CLI row as the saved setting', async () => {
    acpBackendsMock.mockResolvedValue({ backends: rows, kiro_selection_runs_on_kas: true })
    wrap()
    expect((await screen.findByTestId('kiro-runs-on-kas-states')).textContent).toBe(
      'Your setting: Kiro CLI · New sessions use: Kiro Agent',
    )
    const marker = await screen.findByTestId('kiro-row-your-setting')
    expect(marker.textContent).toBe('Your setting')
    // On the Kiro CLI row, not the Kiro Agent one that shows "In use".
    const kiroRow = screen.getByRole('radio', { name: 'Kiro CLI' }).closest('label') as HTMLElement
    expect(kiroRow.contains(marker)).toBe(true)
    // Both go once the setting is saved.
    fireEvent.click(screen.getByRole('button', { name: 'Save Kiro Agent as my setting' }))
    await screen.findByTestId('kiro-runs-on-kas-saved')
    expect(screen.queryByTestId('kiro-runs-on-kas-states')).toBeNull()
    expect(screen.queryByTestId('kiro-row-your-setting')).toBeNull()
  })

  it('brings the callout back when Kiro CLI is picked again after saving Kiro Agent', async () => {
    acpBackendsMock.mockResolvedValue({ backends: rows, kiro_selection_runs_on_kas: true })
    wrap()
    fireEvent.click(
      await screen.findByRole('button', { name: 'Save Kiro Agent as my setting' }),
    )
    await screen.findByTestId('kiro-runs-on-kas-saved')
    // Pick Kiro CLI again on the same panel: the stored pick is Kiro CLI once more.
    fireEvent.click(screen.getByRole('radio', { name: 'Kiro CLI' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Use Kiro CLI' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenLastCalledWith('agent.acp_backend', 'kiro'))
    expect(await screen.findByTestId('kiro-runs-on-kas')).toBeTruthy()
    expect(await screen.findByTestId('kiro-row-your-setting')).toBeTruthy()
    expect(screen.queryByTestId('kiro-runs-on-kas-saved')).toBeNull()
  })

  it('keeps the callout when another harness is checked again', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [...rows.slice(0, 2), probeRow('claude', { installed: 'missing' })],
      kiro_selection_runs_on_kas: true,
    })
    acpBackendRecheckMock.mockResolvedValue({ backend: probeRow('claude') })
    wrap()
    await screen.findByTestId('kiro-runs-on-kas')
    fireEvent.click(screen.getByRole('radio', { name: 'Claude Code' }))
    fireEvent.click(await screen.findByRole('button', { name: /check again/i }))
    await waitFor(() => expect(acpBackendRecheckMock).toHaveBeenCalledWith('claude'))
    await waitFor(() => expect(screen.queryByRole('button', { name: /checking/i })).toBeNull())
    expect(screen.getByTestId('kiro-runs-on-kas')).toBeTruthy()
  })

  it('shows nothing when the setting already says what runs', async () => {
    acpBackendsMock.mockResolvedValue({ backends: rows, kiro_selection_runs_on_kas: false })
    wrap()
    await screen.findAllByRole('radio')
    await waitFor(() => expect(acpBackendsMock).toHaveBeenCalled())
    expect(screen.queryByTestId('kiro-runs-on-kas')).toBeNull()
  })

  it('shows nothing on a gateway that predates the field', async () => {
    acpBackendsMock.mockResolvedValue({ backends: rows })
    wrap()
    await screen.findAllByRole('radio')
    expect(screen.queryByTestId('kiro-runs-on-kas')).toBeNull()
  })
})
