/**
 * The Agent Harness panel says what Crew bots with no pin of their own run on,
 * and which setting decided it (`GET /api/acp-backends` `crewmate_harness`). A
 * fallback (Crew bots cannot run on the picked harness) is shown as an alert.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

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
  acpBackendsMock.mockReset()
  schemaMock.mockReturnValue(schemaWith(['kiro', 'claude', 'kas']))
})

describe('the Crew bot harness line', () => {
  it('names the harness Crew bots follow from this panel', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: rows,
      crewmate_harness: { harness: 'claude', source: 'settings', settings_harness: 'claude' },
    })
    wrap()
    const line = await screen.findByTestId('crewmate-harness')
    expect(line.dataset.source).toBe('settings')
    expect(line.textContent).toBe(
      'Bots run on Claude Code, the harness picked here. A bot can pin its own in its settings.',
    )
    expect(line.getAttribute('role')).toBeNull()
  })

  it('names the legacy setting when it decides', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: rows,
      crewmate_harness: { harness: 'kas', source: 'legacy', settings_harness: 'claude' },
    })
    wrap()
    const line = await screen.findByTestId('crewmate-harness')
    expect(line.textContent).toContain('agent.member_acp_backend')
    expect(line.textContent).toContain('Kiro Agent')
  })

  it('raises an alert naming both harnesses on a fallback', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: rows,
      crewmate_harness: { harness: 'kas', source: 'fallback', settings_harness: 'claude' },
    })
    wrap()
    const line = await screen.findByTestId('crewmate-harness')
    expect(line.getAttribute('role')).toBe('alert')
    expect(line.textContent).toBe(
      "Bots can't run on Claude Code, so they run on Kiro Agent instead. A bot can pin its own harness in its settings.",
    )
  })

  it('shows nothing on a gateway that predates the field', async () => {
    acpBackendsMock.mockResolvedValue({ backends: rows })
    wrap()
    await screen.findAllByRole('radio')
    await waitFor(() => expect(acpBackendsMock).toHaveBeenCalled())
    expect(screen.queryByTestId('crewmate-harness')).toBeNull()
  })
})
