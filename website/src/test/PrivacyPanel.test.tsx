import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { PrivacyPanel } from '../pages/settings/PrivacyPanel'

const HEARTBEAT_DISCLOSURE = 'By default, KiroCrew sends at most one heartbeat per day. Its fixed payload contains a random installation ID, app version, release channel, operating system, CPU architecture, Python minor version, installation channel, governance posture, and a first-run flag. The ID identifies an installed copy, not a person.'

const HEARTBEAT_FIELDS = [
  'random installation ID',
  'app version',
  'release channel',
  'operating system',
  'CPU architecture',
  'Python minor version',
  'installation channel',
  'governance posture',
  'first-run flag',
] as const

const EXCLUSION_DISCLOSURE = 'The heartbeat never includes prompts, model responses, file contents or paths, repository or branch names, credentials, environment variables, hostnames, or usernames. The receiving service does not retain client IP addresses.'

const CONTROL_COMMANDS = [
  'kirocrew telemetry status',
  'kirocrew telemetry disable',
  'export KIROCREW_TELEMETRY_DISABLED=1',
  "$env:KIROCREW_TELEMETRY_DISABLED = '1'",
  'set KIROCREW_TELEMETRY_DISABLED=1',
] as const

const SHELL_COMMANDS = [
  ['macOS / Linux', 'export KIROCREW_TELEMETRY_DISABLED=1'],
  ['Windows PowerShell', "$env:KIROCREW_TELEMETRY_DISABLED = '1'"],
  ['Windows Command Prompt', 'set KIROCREW_TELEMETRY_DISABLED=1'],
] as const

describe('PrivacyPanel', () => {
  it('uses semantic headings for every disclosure section', () => {
    render(<PrivacyPanel />)

    const panel = screen.getByLabelText('Privacy')
    const headings = within(panel).getAllByRole('heading', { level: 3 })

    expect(headings.map(heading => heading.textContent)).toEqual([
      'Anonymous daily heartbeat',
      'What telemetry never includes',
      'Data that stays local',
      'Disable telemetry from the command line',
    ])
  })

  it('discloses exactly the fixed nine-field heartbeat payload', () => {
    render(<PrivacyPanel />)

    const disclosure = screen.getByText(HEARTBEAT_DISCLOSURE)
    expect(HEARTBEAT_FIELDS).toHaveLength(9)
    for (const field of HEARTBEAT_FIELDS) {
      expect(disclosure).toHaveTextContent(field)
    }
  })

  it('pins the excluded data and IP-retention disclosure', () => {
    render(<PrivacyPanel />)

    expect(screen.getByText(EXCLUSION_DISCLOSURE)).toBeInTheDocument()
  })

  it('shows persistent and labelled cross-platform telemetry controls', () => {
    render(<PrivacyPanel />)

    const controlsHeading = screen.getByRole('heading', {
      name: 'Disable telemetry from the command line',
    })
    const controlsCard = controlsHeading.parentElement
    expect(controlsCard).not.toBeNull()

    const commands = Array.from(controlsCard!.querySelectorAll('code'))
      .map(command => command.textContent)
    expect(commands).toEqual(CONTROL_COMMANDS)

    for (const [label, command] of SHELL_COMMANDS) {
      const labelElement = within(controlsCard!).getByText(label)
      expect(labelElement.parentElement).toHaveTextContent(command)
    }
  })
})
