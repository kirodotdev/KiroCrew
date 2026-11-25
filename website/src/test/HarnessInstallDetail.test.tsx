/**
 * `HarnessInstallDetail`: Kiro CLI gets the platform install commands and its
 * setup page; any other harness shows the probe's own command.
 */
import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { HarnessInstallDetail, KIRO_CLI_INSTALL_DOCS_URL } from '../components/agentHarness'
import { renderWithProviders } from './helpers'
import { probe } from './KiroPrerequisiteGateTestUtils'

describe('HarnessInstallDetail', () => {
  it('shows Kiro CLI the install command for the gateway platform and its setup page', () => {
    renderWithProviders(
      <HarnessInstallDetail
        value="kiro"
        name="Kiro CLI"
        probe={probe({ id: 'kiro', installed: 'missing', install_command: '' })}
        prerequisite={{ platform: 'Linux', unzip_missing: false }}
      />,
    )
    expect(screen.getAllByText(/cli\.kiro\.dev\/install/).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('link').map(a => a.getAttribute('href'))).toContain(KIRO_CLI_INSTALL_DOCS_URL)
  })

  it('shows another harness its own install command', () => {
    renderWithProviders(
      <HarnessInstallDetail
        value="claude"
        name="Claude Code"
        probe={probe({ id: 'claude', installed: 'missing', install_command: 'npm i -g claude-agent-acp' })}
      />,
    )
    expect(screen.getByText('npm i -g claude-agent-acp')).toBeInTheDocument()
  })
})
